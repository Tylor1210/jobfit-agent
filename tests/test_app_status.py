from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobfit.applications.models import Application
from jobfit.applications.status import GHOST_DAYS, is_ghosted, next_status
from jobfit.applications.store import InMemoryAppStore, stage_after


@pytest.mark.parametrize(
    ("cur", "kind", "want"),
    [
        ("applied", "assessment", "screening"),
        ("applied", "interview_request", "interview"),
        ("screening", "interview_request", "interview"),
        ("interview", "offer", "offer"),
        ("interview", "assessment", "interview"),  # never backward
        ("interview", "application_received", "interview"),
        ("applied", "rejection", "rejected"),
        ("offer", "rejection", "rejected"),
        ("rejected", "application_received", "rejected"),  # resend doesn't reopen
        ("rejected", "offer", "offer"),
        ("withdrawn", "offer", "withdrawn"),
        ("ghosted", "interview_request", "interview"),  # revived
        ("ghosted", "rejection", "rejected"),
        ("applied", "recruiter_outreach", "applied"),
        ("applied", "other", "applied"),
    ],
)
def test_transitions(cur: str, kind: str, want: str) -> None:
    assert next_status(cur, kind) == want  # type: ignore[arg-type]


def test_ghosting_after_21_days() -> None:
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    assert not is_ghosted("applied", t0, t0 + timedelta(days=GHOST_DAYS - 1))
    assert is_ghosted("applied", t0, t0 + timedelta(days=GHOST_DAYS))
    assert is_ghosted("interview", t0, t0 + timedelta(days=30))
    assert not is_ghosted("offer", t0, t0 + timedelta(days=90))
    assert not is_ghosted("rejected", t0, t0 + timedelta(days=90))


def test_store_marks_ghosted() -> None:
    st = InMemoryAppStore()
    old = datetime.now(UTC) - timedelta(days=30)
    st.save_app(Application(company="A", source="email", last_activity_at=old))
    st.save_app(Application(company="B", source="email"))
    st.save_app(Application(company="C", source="email", status="offer", last_activity_at=old))
    assert st.mark_ghosted(datetime.now(UTC)) == 1
    assert sorted(a.status for a in st.list_apps()) == ["applied", "ghosted", "offer"]


@pytest.mark.parametrize(
    ("cur", "target", "want"),
    [
        ("new", "applied", "applied"),
        ("shortlisted", "interview", "interview"),
        ("interview", "applied", None),  # never backward
        ("applied", "applied", None),
        ("closed", "applied", None),  # hand-parked
        ("applied", "rejected", "rejected"),
        ("offer", "rejected", None),
    ],
)
def test_job_card_stage(cur: str, target: str, want: str | None) -> None:
    assert stage_after(cur, target) == want
