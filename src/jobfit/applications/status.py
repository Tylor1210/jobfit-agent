"""Application status state machine. Pure functions; the mail graph and the API call these.

applied -> screening -> interview -> offer, plus rejected / withdrawn (terminal) and ghosted
(no activity for GHOST_DAYS while still in progress). Email events only move forward; a new
event on a ghosted application revives it. Manual edits bypass this and can set anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from jobfit.applications.models import AppStatus

GHOST_DAYS = 21
_ORDER: dict[AppStatus, int] = {"applied": 0, "screening": 1, "interview": 2, "offer": 3}
TERMINAL: frozenset[AppStatus] = frozenset({"rejected", "withdrawn"})
IN_PROGRESS: frozenset[AppStatus] = frozenset({"applied", "screening", "interview"})

# Mail kind -> the status it implies. recruiter_outreach / other imply nothing.
EVENT_STATUS: dict[str, AppStatus] = {
    "application_received": "applied",
    "assessment": "screening",
    "interview_request": "interview",
    "offer": "offer",
    "rejection": "rejected",
}


def next_status(current: AppStatus, event_kind: str) -> AppStatus:
    target = EVENT_STATUS.get(event_kind)
    if target is None:
        return current
    if current in TERMINAL:
        # WHY: a stray "thanks for applying" resend after a rejection must not reopen it.
        # Only an offer outranks a rejection (it happens: a second req at the same company).
        return "offer" if target == "offer" and current == "rejected" else current
    if current == "ghosted":
        return target  # any new activity revives it, rejection included
    if target in TERMINAL:
        return target
    return target if _ORDER[target] > _ORDER.get(current, -1) else current


def is_ghosted(status: AppStatus, last_activity_at: datetime, at: datetime) -> bool:
    return status in IN_PROGRESS and at - last_activity_at >= timedelta(days=GHOST_DAYS)
