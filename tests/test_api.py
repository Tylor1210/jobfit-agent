"""API end to end against Postgres with a scripted LLM: add, score, move, tailor, edit, approve,
download. Needs DATABASE_URL (local pgvector or the CI service)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs DATABASE_URL"),
]


@pytest.fixture
def client(tmp_path: Path) -> Any:
    from factories import letter, plan, resume
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import InMemorySaver
    from test_graphs import _script_fit

    from jobfit.api.app import create_app
    from jobfit.api.queries import Dashboard
    from jobfit.graphs.common import Deps
    from jobfit.llm.client import LLM, ModelsConfig
    from jobfit.llm.cost import InMemoryLedger
    from jobfit.llm.fake import FakeProvider
    from jobfit.profile.chunks import build_chunks
    from jobfit.profile.loader import load_constraints, load_profile
    from jobfit.rag.embedder import HashEmbedder
    from jobfit.rag.store import InMemoryStore
    from jobfit.schemas import CoverLetterOut, EmphasisOut, ResumeOut
    from jobfit.store.repo import PgRepo
    from jobfit.tracing import MemoryTracer

    dsn = os.environ["DATABASE_URL"]
    fake = FakeProvider()
    _script_fit(fake)
    fake.script(EmphasisOut, plan()).script(ResumeOut, resume()).script(CoverLetterOut, letter())
    store = InMemoryStore(HashEmbedder())
    profile = load_profile()
    store.upsert(build_chunks(profile))

    def render(r: Any, c: Any, j: Any) -> tuple[str, str]:
        a, b = tmp_path / "r.pdf", tmp_path / "c.pdf"
        a.write_bytes(b"%PDF-1.4 resume")
        b.write_bytes(b"%PDF-1.4 cover")
        return str(a), str(b)

    deps = Deps(
        llm=LLM(fake, ModelsConfig.load(), InMemoryLedger(), MemoryTracer()),
        store=store,
        repo=PgRepo(dsn),
        profile=profile,
        constraints=load_constraints(),
        page_count=lambda r: 1,
        render=render,
        verify_with_llm=False,
    )
    app = create_app(
        deps=deps, dashboard=Dashboard(dsn), checkpointer=InMemorySaver(), serve_web=False
    )
    with TestClient(app) as c:
        yield c
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("delete from jobs where company = 'PytestCo'")


def _wait(client: Any, jid: str, until: Any, timeout: float = 20) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        d: dict[str, Any] = client.get(f"/api/jobs/{jid}").json()
        if until(d):
            return d
        time.sleep(0.1)
    raise AssertionError(f"timed out; last state: {d.get('tailor_status')} {d.get('task')}")


POSTING = (
    "Forward Deployed Engineer. Build AI agent workflows with tool calling for "
    "enterprise clients. 3+ years building production software. Python and TypeScript. "
    "LLM APIs. Client-facing communication. Nice to have: Kubernetes. "
) * 2


def test_full_flow(client: Any) -> None:
    r = client.post(
        "/api/jobs",
        json={"text": POSTING, "company": "PytestCo", "title": "Forward Deployed Engineer"},
    )
    assert r.status_code == 202
    jid = r.json()["id"]
    d = _wait(client, jid, lambda d: d["fit"] is not None)
    assert d["fit"]["verdict"] == "strong" and d["task"]["status"] == "done"
    assert any(j["id"] == jid for j in client.get("/api/jobs").json())

    assert client.patch(f"/api/jobs/{jid}/stage", json={"stage": "shortlisted"}).status_code == 200
    assert client.patch(f"/api/jobs/{jid}/stage", json={"stage": "bogus"}).status_code == 422

    assert client.post(f"/api/jobs/{jid}/review", json={"action": "approve"}).status_code == 404
    assert client.post(f"/api/jobs/{jid}/tailor").status_code == 202
    d = _wait(
        client,
        jid,
        lambda d: d["tailor_status"] == "awaiting_review" and d["task"]["status"] == "done",
    )
    assert d["drafts"]["resume"]["summary"] and d["issues"] == []

    edited = dict(d["drafts"]["resume"], summary=d["drafts"]["resume"]["summary"] + " Ships fast.")
    assert (
        client.post(
            f"/api/jobs/{jid}/review", json={"action": "edit", "resume": edited}
        ).status_code
        == 202
    )
    d = _wait(
        client,
        jid,
        lambda d: d["tailor_status"] == "awaiting_review" and d["task"]["status"] == "done",
    )
    assert d["drafts"]["resume"]["summary"].endswith("Ships fast.")

    assert client.post(f"/api/jobs/{jid}/review", json={"action": "revise"}).status_code == 400
    assert client.post(f"/api/jobs/{jid}/review", json={"action": "approve"}).status_code == 202
    d = _wait(client, jid, lambda d: d["tailor_status"] == "rendered")
    assert d["has_resume_pdf"] and d["has_cover_pdf"] and "resume_pdf" not in d
    pdf = client.get(f"/api/jobs/{jid}/pdf/resume")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    gaps = client.get("/api/gaps").json()
    assert isinstance(gaps, list)
    ops = client.get("/api/ops").json()
    assert {"runs", "spend", "evals", "traces", "counts", "cap_usd"} <= set(ops)
    assert "sum.1" in client.get("/api/evidence").json()


def test_add_job_rejects_linkedin(client: Any) -> None:
    r = client.post("/api/jobs", json={"url": "https://www.linkedin.com/jobs/view/1"})
    assert r.status_code == 400 and "Paste the posting text" in r.json()["detail"]
