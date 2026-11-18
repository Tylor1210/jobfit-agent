"""Application tracking models: applications, their events, mail messages, and LLM outputs.

Used by the mail graph (graphs/mail.py), the quick-add parser, the store, and the API.
GOTCHA: like schemas.py, models the LLM fills (suffix `Out`) carry no Field constraints;
limits are enforced in code after parsing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

AppStatus = Literal[
    "applied", "screening", "interview", "offer", "rejected", "withdrawn", "ghosted"
]
APP_STATUSES: tuple[AppStatus, ...] = (
    "applied",
    "screening",
    "interview",
    "offer",
    "rejected",
    "withdrawn",
    "ghosted",
)
AppSource = Literal["email", "manual", "quick_add", "follow_up"]
Channel = Literal["linkedin", "company_site", "recruiter", "referral", "other"]
MailKind = Literal[
    "application_received",
    "rejection",
    "interview_request",
    "assessment",
    "recruiter_outreach",
    "offer",
    "other",
]
EventKind = Literal[
    "application_received",
    "rejection",
    "interview_request",
    "assessment",
    "recruiter_outreach",
    "offer",
    "other",
    "manual_update",
    "status_change",
]

SNIPPET_MAX = 200  # CONSTRAINT: matches the DB check; full bodies are never stored


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> datetime:
    return datetime.now(UTC)


class Application(BaseModel):
    id: str = Field(default_factory=new_id)
    company: str
    role: str | None = None
    status: AppStatus = "applied"
    source: AppSource
    channel: Channel = "other"
    applied_at: datetime | None = None
    last_activity_at: datetime = Field(default_factory=now)
    job_id: str | None = None
    resume_version: str | None = None
    salary_entered: str | None = None
    notes: str | None = None
    needs_review: bool = False
    review_reason: str | None = None
    created_at: datetime = Field(default_factory=now)


class AppEvent(BaseModel):
    id: str = Field(default_factory=new_id)
    application_id: str | None = None
    company: str | None = None
    kind: EventKind
    occurred_at: datetime = Field(default_factory=now)
    account: str | None = None
    message_id: str | None = None
    snippet: str | None = None
    extracted: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = None
    injection_flags: list[str] = Field(default_factory=list)
    needs_review: bool = False
    review_reason: str | None = None


class MailMessage(BaseModel):
    """One fetched email. `body` lives only in memory for the classify call; never persisted."""

    account: str
    message_id: str
    sender: str
    subject: str
    received_at: datetime
    body: str


# ---- LLM outputs ------------------------------------------------------------------------------


class MailClassOut(BaseModel):
    kind: MailKind
    company: str | None = Field(
        description="The hiring company (not the ATS vendor or job board). Null if not stated."
    )
    role: str | None = Field(
        description="The job title exactly as written in the email. Null if the email does not "
        "name a specific role. Never infer or guess one."
    )
    next_step: str | None = Field(description="What the sender asks you to do next, short.")
    deadline: str | None = Field(description="Deadline as YYYY-MM-DD if one is stated, else null.")
    confidence: float = Field(description="0 to 1: how sure you are of kind and company.")


class ApplicationDraftOut(BaseModel):
    company: str = Field(description="Company name as typed, with normal capitalization.")
    role: str | None = Field(description="Job title in Title Case, or null if not given.")
    channel: Channel | None = Field(description="Where the application was sent, if stated.")
    salary_entered: str | None = Field(description="Salary figure entered, e.g. '85k'.")
    applied_on: str | None = Field(description="YYYY-MM-DD if a date is given, else null.")
    notes: str | None = Field(description="Anything else in the line, short. Null if nothing.")
