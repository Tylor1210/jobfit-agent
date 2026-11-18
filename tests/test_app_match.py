from __future__ import annotations

from jobfit.applications.match import (
    company_score,
    match_application,
    match_job,
    norm_company,
    role_score,
)
from jobfit.applications.models import AppEvent, Application
from jobfit.applications.store import InMemoryAppStore


def _app(company: str, role: str | None = None) -> Application:
    return Application(company=company, role=role, source="email")


def test_norm_company() -> None:
    assert norm_company("Acme Robotics, Inc.") == "acme robotics"
    assert norm_company("Northwind & Co") == "northwind and"
    assert norm_company("The Larkspur Group LLC") == "larkspur"


def test_company_score() -> None:
    assert company_score("Acme Robotics Inc", "acme robotics") == 100
    assert company_score("Acme", "Acme Robotics") >= 90
    assert company_score("Scale AI", "AI") < 90  # short generic names don't merge
    assert company_score("Northwind", "Northwinds") >= 90
    assert company_score("Northwind", "Southwind") < 90


def test_role_score() -> None:
    assert role_score("Sr. AI Engineer", "Senior AI Engineer") >= 95
    assert role_score("AI Engineer", "Senior AI Engineer (Remote)") >= 80
    assert role_score("AI Engineer", "Data Engineer") < 80
    assert role_score("Forward Deployed Engineer", "FDE") >= 80
    assert role_score("Solutions Architect", "Software Engineer") < 80


def test_match_by_company_and_role() -> None:
    a1, a2 = _app("Acme", "AI Engineer"), _app("Acme", "Data Engineer")
    r = match_application("Acme Inc", "Senior AI Engineer", [a1, a2])
    assert r.kind == "matched" and r.app is a1


def test_new_role_at_known_company_is_not_a_match() -> None:
    r = match_application("Acme", "Product Manager", [_app("Acme", "AI Engineer")])
    assert r.kind == "none"


def test_vague_with_one_application_links_it() -> None:
    a = _app("Acme", "AI Engineer")
    r = match_application("Acme", None, [a, _app("Globex", "AI Engineer")])
    assert r.kind == "company_only" and r.app is a


def test_vague_with_two_applications_is_ambiguous() -> None:
    r = match_application("Acme", None, [_app("Acme", "AI Engineer"), _app("Acme", "SRE")])
    assert r.kind == "ambiguous" and r.app is None and len(r.candidates) == 2


def test_role_fills_a_roleless_application() -> None:
    a = _app("Acme")
    r = match_application("Acme", "AI Engineer", [a])
    assert r.kind == "matched" and r.app is a


def test_match_job() -> None:
    jobs = [
        {"id": "j1", "company": "Acme Robotics", "title": "AI Engineer", "stage": "new"},
        {"id": "j2", "company": "Acme Robotics", "title": "Data Engineer", "stage": "new"},
    ]
    assert match_job("Acme", "Senior AI Engineer", jobs) == jobs[0]
    assert match_job("Acme", None, jobs) is None
    assert match_job("Globex", "AI Engineer", jobs) is None


def test_dedup_by_message_id() -> None:
    st = InMemoryAppStore()
    assert st.add_event(AppEvent(kind="rejection", message_id="<m1@x>"))
    assert not st.add_event(AppEvent(kind="rejection", message_id="<m1@x>"))
    assert st.add_event(AppEvent(kind="manual_update"))  # no message id: never a dup
    assert st.add_event(AppEvent(kind="manual_update"))
    assert st.has_message("<m1@x>") and len(st.events) == 3
