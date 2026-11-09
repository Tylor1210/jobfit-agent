"""Vector store behaviour. HashEmbedder offline; pgvector test needs DATABASE_URL."""

from __future__ import annotations

import os

import pytest

from jobfit.profile.chunks import build_chunks
from jobfit.profile.loader import load_profile
from jobfit.rag.embedder import HashEmbedder
from jobfit.rag.store import InMemoryStore, PgVectorStore


def test_inmemory_retrieves_relevant_evidence() -> None:
    store = InMemoryStore(HashEmbedder())
    chunks = build_chunks(load_profile())
    assert store.upsert(chunks) == len(chunks)
    assert store.upsert(chunks) == 0  # unchanged chunks are not re-embedded

    ids = [h.id for h in store.search("Python trading engine backtesting", k=3)]
    assert "exp.tycodes.4" in ids

    skills = store.search("TypeScript", k=3, kinds=["skill"])
    assert skills[0].id == "skill.typescript"
    assert all(h.kind == "skill" for h in skills)


@pytest.mark.db
@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs DATABASE_URL")
def test_pgvector_roundtrip() -> None:
    import psycopg

    dsn = os.environ["DATABASE_URL"]
    # WHY: own table so the test never overwrites the real index with hash vectors.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("drop table if exists profile_chunks_test")
        conn.execute("create table profile_chunks_test (like profile_chunks including all)")
    try:
        store = PgVectorStore(dsn, HashEmbedder(), table="profile_chunks_test")
        chunks = build_chunks(load_profile())
        assert store.upsert(chunks) == len(chunks)
        assert store.upsert(chunks) == 0
        hits = store.search("Python trading engine backtesting", k=3)
        assert "exp.tycodes.4" in [h.id for h in hits]
        assert all(h.kind == "skill" for h in store.search("Python", k=3, kinds=["skill"]))
        many = store.search_many("Python trading engine", {"bullet": 3, "skill": 2})
        assert sorted(h.kind for h in many) == ["bullet", "bullet", "bullet", "skill", "skill"]
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("drop table if exists profile_chunks_test")


def test_search_evidence_returns_each_kind() -> None:
    from jobfit.rag.store import search_evidence

    store = InMemoryStore(HashEmbedder())
    store.upsert(build_chunks(load_profile()))
    hits = search_evidence(store, "Python data validation", {"bullet": 2, "skill": 2})
    assert {h.kind for h in hits} == {"bullet", "skill"}
    assert len(hits) == 4


@pytest.mark.db
@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs DATABASE_URL")
def test_pg_repo_ledger_and_checkpointer() -> None:
    import uuid

    import psycopg
    from factories import letter, resume
    from langgraph.checkpoint.postgres import PostgresSaver

    from jobfit.llm.cost import PgLedger, UsageRecord
    from jobfit.schemas import Drafts, FitResult, JobPosting
    from jobfit.store.repo import PgRepo

    dsn = os.environ["DATABASE_URL"]
    repo = PgRepo(dsn)
    ext = uuid.uuid4().hex[:8]
    job = JobPosting(
        id=f"test:{ext}",
        source="test",
        external_id=ext,
        company="Acme",
        title="Engineer",
        description="d",
    )
    repo.upsert_job(job)
    got = repo.get_job(job.id)
    assert got is not None and got.first_seen is not None
    seen = {"first_seen", "last_seen"}
    assert got.model_dump(exclude=seen) == job.model_dump(exclude=seen)
    fit = FitResult(
        fit_score=80,
        verdict="strong",
        verdict_line="ok",
        assessments=[],
        top_gaps=["x"],
        injection_flags=["override_instructions: ..."],
    )
    repo.save_fit(job.id, fit)
    assert repo.get_fit(job.id) == fit
    repo.set_stage(job.id, "shortlisted")
    assert repo.get_job(job.id).stage == "shortlisted"  # type: ignore[union-attr]
    repo.save_tailor(
        job.id,
        "t1",
        "awaiting_review",
        drafts=Drafts(resume=resume(), cover_letter=letter()),
        issues=[],
    )
    repo.save_tailor(job.id, "t1", "rendered", resume_pdf="/a.pdf", cover_pdf="/b.pdf")
    with pytest.raises(ValueError):
        repo.save_tailor(job.id, "t1", "x", **{"status; drop table jobs": 1})

    ledger = PgLedger(dsn)
    before = ledger.spent_today()
    ledger.record(UsageRecord("pytest", "n", "fake", "m", 10, 5, 0.25, 3))
    try:
        assert ledger.spent_today() == pytest.approx(before + 0.25)
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("delete from llm_usage where run_id = 'pytest'")
            conn.execute("delete from jobs where source = 'test'")

    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.setup()
