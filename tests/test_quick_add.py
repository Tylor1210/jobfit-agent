"""Quick-add parsing and the applications API, offline (FakeProvider, in-memory stores)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobfit.api.applications import build_router
from jobfit.api.tasks import TaskRunner
from jobfit.applications.models import AppEvent, Application, ApplicationDraftOut
from jobfit.applications.quick_add import parse_quick_add, prepass
from jobfit.applications.store import InMemoryAppStore
from jobfit.graphs.common import Deps
from jobfit.llm.fake import FakeProvider
from jobfit.schemas import JobPosting

TODAY = date(2026, 11, 16)


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ("applied Northwind junior AI engineer, put 85k", {"salary_entered": "85k"}),
        (
            "applied Acme SRE $120,000 via linkedin",
            {"salary_entered": "$120,000", "channel": "linkedin"},
        ),
        (
            "Globex data eng yesterday, referral",
            {"applied_on": date(2026, 11, 15), "channel": "referral"},
        ),
        (
            "Initech on 11/12 company site 95K",
            {"applied_on": date(2026, 11, 12), "channel": "company_site", "salary_entered": "95K"},
        ),
        ("Hooli 12/20", {"applied_on": date(2025, 12, 20)}),  # future date -> last year
        ("Acme backend engineer", {}),
    ],
)
def test_prepass(line: str, want: dict[str, Any]) -> None:
    assert prepass(line, TODAY) == want


def _draft(**kw: Any) -> ApplicationDraftOut:
    base: dict[str, Any] = {
        "company": "Northwind",
        "role": "Junior AI and Automation Engineer",
        "channel": None,
        "salary_entered": None,
        "applied_on": None,
        "notes": None,
    }
    return ApplicationDraftOut(**{**base, **kw})


def test_parse_merges_regex_and_model(deps: Deps, fake: FakeProvider) -> None:
    fake.script(ApplicationDraftOut, _draft(salary_entered="85", channel="recruiter"))
    d = parse_quick_add(
        "applied Northwind junior AI and automation engineer, put 85k via linkedin", deps, TODAY
    )
    assert (d.company, d.role, d.salary_entered, d.channel, d.applied_on) == (
        "Northwind",
        "Junior AI and Automation Engineer",
        "85k",
        "linkedin",
        TODAY,
    )


def test_parse_needs_a_company(deps: Deps, fake: FakeProvider) -> None:
    fake.script(ApplicationDraftOut, _draft(company=" "))
    with pytest.raises(ValueError, match="company"):
        parse_quick_add("put 85k", deps, TODAY)
    with pytest.raises(ValueError):
        parse_quick_add("   ", deps, TODAY)


# ---- API --------------------------------------------------------------------------------------


@pytest.fixture
def api(deps: Deps) -> Iterator[tuple[TestClient, InMemoryAppStore]]:
    assert hasattr(deps.repo, "jobs")
    store = InMemoryAppStore(deps.repo)  # type: ignore[arg-type]
    app = FastAPI()
    app.include_router(build_router(lambda: (deps, store), TaskRunner()))
    with TestClient(app) as c:
        yield c, store


def test_quick_add_preview_then_confirm(
    api: tuple[TestClient, InMemoryAppStore], fake: FakeProvider, deps: Deps
) -> None:
    c, store = api
    deps.repo.upsert_job(
        JobPosting(
            id="gh:1",
            source="greenhouse",
            external_id="1",
            company="Northwind",
            title="AI Automation Engineer",
            description="x",
        )
    )
    fake.script(ApplicationDraftOut, _draft(role="AI Automation Engineer"))
    draft = c.post(
        "/api/applications/parse",
        json={"text": "applied northwind ai automation engineer, put 85k"},
    ).json()
    assert draft["salary_entered"] == "85k" and store.list_apps() == []  # preview only
    r = c.post("/api/applications", json={**draft, "source": "quick_add"})
    assert r.status_code == 201
    (app,) = store.list_apps()
    assert app.source == "quick_add" and app.job_id == "gh:1"
    assert deps.repo.get_job("gh:1").stage == "applied"  # type: ignore[union-attr]
    detail = c.get(f"/api/applications/{app.id}").json()
    assert detail["job"]["id"] == "gh:1" and detail["events"][0]["snippet"] == "Quick add"


def test_full_form_filters_and_status_edit(api: tuple[TestClient, InMemoryAppStore]) -> None:
    c, store = api
    c.post(
        "/api/applications",
        json={
            "company": "Acme",
            "role": "SRE",
            "applied_on": "2026-10-01",
            "channel": "company_site",
            "resume_version": "v3",
            "salary_entered": "110k",
        },
    )
    c.post("/api/applications", json={"company": "Globex", "applied_on": "2026-11-10"})
    assert len(c.get("/api/applications").json()) == 2
    assert [a["company"] for a in c.get("/api/applications?company=acme").json()] == ["Acme"]
    assert len(c.get("/api/applications?date_from=2026-11-01").json()) == 1
    assert len(c.get("/api/applications?source=manual&status=applied").json()) == 2
    aid = c.get("/api/applications?company=acme").json()[0]["id"]
    r = c.patch(f"/api/applications/{aid}", json={"status": "withdrawn", "notes": "took another"})
    assert r.json()["status"] == "withdrawn"
    kinds = [e.kind for e in store.events_for(aid)]
    assert kinds == ["manual_update", "status_change"]


def test_review_queue_assign_create_dismiss(api: tuple[TestClient, InMemoryAppStore]) -> None:
    c, store = api
    a = Application(company="Acme", role="SRE", source="manual")
    store.save_app(a)
    for i in range(3):
        store.add_event(
            AppEvent(
                kind="rejection",
                company="Acme",
                message_id=f"<r{i}>",
                needs_review=True,
                review_reason="role not stated",
            )
        )
    q = c.get("/api/applications/review").json()
    e1, e2, e3 = (e["id"] for e in q["events"])
    r = c.post(f"/api/applications/review/{e1}", json={"action": "assign", "application_id": a.id})
    assert r.json()["application"]["status"] == "rejected"
    r = c.post(f"/api/applications/review/{e2}", json={"action": "create", "role": "Data Eng"})
    assert r.json()["application"]["source"] == "follow_up"
    c.post(f"/api/applications/review/{e3}", json={"action": "dismiss"})
    assert c.get("/api/applications/review").json()["events"] == []
    assert c.get("/api/applications/summary").json()["total"] == 2


def test_parse_reports_provider_trouble_as_503(
    api: tuple[TestClient, InMemoryAppStore], deps: Deps
) -> None:
    from jobfit.llm.cost import UsageRecord

    deps.llm.ledger.record(UsageRecord("x", "n", "fake", "m", 1, 1, 999.0, 1))
    r = api[0].post("/api/applications/parse", json={"text": "applied Acme sre"})
    assert r.status_code == 503
