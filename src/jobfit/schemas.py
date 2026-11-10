"""Shared data models: job postings, LLM structured outputs, fit results, and drafts.

GOTCHA: models the LLM fills (suffix `Out`) avoid Field constraints (min/max, patterns), because
structured-output schemas reject or silently drop many of them. Limits are enforced in code
after parsing instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Stage = Literal["new", "shortlisted", "applied", "interview", "offer", "rejected", "closed"]
# "unknown": the posting had too little text to extract requirements (e.g. Remote OK snippets).
Verdict = Literal["strong", "moderate", "weak", "not_a_fit", "unknown"]
Status = Literal["met", "partial", "missing"]
ReqCategory = Literal[
    "technical_skill",
    "tool",
    "domain",
    "years_experience",
    "education",
    "certification",
    "soft_skill",
    "clearance",
    "location",
    "other",
]


class JobPosting(BaseModel):
    id: str
    source: str
    external_id: str
    company: str
    title: str
    location: str | None = None
    url: str | None = None
    description: str
    pay: str | None = None
    posted_at: datetime | None = None
    status: Literal["open", "closed"] = "open"
    stage: Stage = "new"
    board: str | None = None  # watchlist token / feed / query the job came from
    remote: bool | None = None
    closes_at: datetime | None = None  # application deadline when the source publishes one
    first_seen: datetime | None = None  # set by the database
    last_seen: datetime | None = None


# ---- LLM outputs: fit graph -------------------------------------------------------------------


class RequirementOut(BaseModel):
    text: str = Field(description="One requirement, short, in the posting's terms.")
    kind: Literal["required", "preferred"]
    category: ReqCategory


class RequirementsOut(BaseModel):
    requirements: list[RequirementOut]


class AssessmentOut(BaseModel):
    req_id: str
    status: Status
    evidence_ids: list[str] = Field(description="IDs from the evidence list only. Empty if none.")
    note: str = Field(description="Under 20 words: why met, partial, or missing.")


class FitOut(BaseModel):
    assessments: list[AssessmentOut]
    verdict_line: str = Field(description="One plain sentence on overall fit.")
    top_gaps: list[str] = Field(description="Up to 3 most important missing or partial items.")


# ---- Fit result (what we store) ---------------------------------------------------------------


class Requirement(BaseModel):
    id: str
    text: str
    kind: Literal["required", "preferred"]
    category: ReqCategory


class EvidenceRef(BaseModel):
    id: str
    text: str
    score: float


class Assessment(BaseModel):
    requirement: Requirement
    status: Status
    evidence: list[EvidenceRef]
    note: str


class FitResult(BaseModel):
    fit_score: int
    verdict: Verdict
    verdict_line: str
    assessments: list[Assessment]
    top_gaps: list[str]
    injection_flags: list[str] = Field(default_factory=list)
    trace_url: str | None = None  # Langfuse trace of the run that produced this result


# ---- LLM outputs: tailor graph ----------------------------------------------------------------


class EmphasisOut(BaseModel):
    angle: str = Field(description="One sentence: how to position Tylor for this job.")
    skill_ids: list[str] = Field(description="Profile skill IDs to feature, most relevant first.")
    bullet_ids: list[str] = Field(description="Profile bullet IDs to keep, most relevant first.")
    posting_hooks: list[str] = Field(
        description="2-3 specific things in the posting that Tylor's evidence directly supports."
    )


class SkillRowOut(BaseModel):
    label: str = Field(description="Row label, 2-4 words, e.g. 'AI & Agent Systems'.")
    skill_ids: list[str]


class DraftBulletOut(BaseModel):
    text: str
    source_ids: list[str] = Field(description="Bullet IDs this line is rewritten from.")


class DraftJobOut(BaseModel):
    job_id: str = Field(description="Experience or military ID, e.g. exp.tycodes or mil.flarng.")
    bullets: list[DraftBulletOut]


class ResumeOut(BaseModel):
    summary: str
    summary_evidence_ids: list[str]
    skill_rows: list[SkillRowOut]
    jobs: list[DraftJobOut]


class ParagraphOut(BaseModel):
    kind: Literal["opening", "evidence", "gap", "closing"]
    text: str
    evidence_ids: list[str]


class CoverLetterOut(BaseModel):
    greeting: str = Field(description="e.g. 'Dear TribalScale Hiring Team,'")
    paragraphs: list[ParagraphOut]


class UnsupportedClaimOut(BaseModel):
    item: int = Field(description="The item number the claim came from.")
    claim: str = Field(description="The unsupported part, quoted.")
    reason: str


class ClaimCheckOut(BaseModel):
    unsupported: list[UnsupportedClaimOut]


class Issue(BaseModel):
    doc: Literal["resume", "cover_letter"]
    rule: str
    detail: str


class Drafts(BaseModel):
    resume: ResumeOut
    cover_letter: CoverLetterOut
