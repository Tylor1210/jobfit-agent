"""Quick-add: one typed line -> a draft application for the user to confirm. Never saves.

"applied Northwind junior AI and automation engineer, put 85k" -> company, role, salary...
A regex pre-pass pulls salary, date and channel deterministically; the LLM (tier fast) only
splits company from role and cleans capitalization. Regex values win over the model's.
Called by POST /api/applications/parse; the confirm step posts the edited draft to create.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from pydantic import BaseModel

from jobfit.applications.models import ApplicationDraftOut, Channel
from jobfit.graphs import prompts
from jobfit.graphs.common import Deps

MAX_LINE = 300

_SALARY = re.compile(
    r"\$\s?\d{2,3}(?:[.,]\d{1,3})?\s?[kK]\b|\b\d{2,3}(?:\.\d)?\s?[kK]\b|\$\s?\d{2,3},\d{3}\b"
    r"|\b\d{2,3},\d{3}\b"
)
_MDY = re.compile(r"\b(?:on\s+)?(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_CHANNELS: list[tuple[Channel, re.Pattern[str]]] = [
    ("linkedin", re.compile(r"\b(linked\s?in|easy apply)\b", re.I)),
    ("referral", re.compile(r"\b(referral|referred)\b", re.I)),
    ("recruiter", re.compile(r"\brecruiter\b", re.I)),
    ("company_site", re.compile(r"\b(company site|website|careers page|career site)\b", re.I)),
]


class QuickAddDraft(BaseModel):
    company: str
    role: str | None = None
    channel: Channel = "other"
    salary_entered: str | None = None
    applied_on: date
    notes: str | None = None


def prepass(line: str, today: date) -> dict[str, object]:
    out: dict[str, object] = {}
    if m := _SALARY.search(line):
        out["salary_entered"] = re.sub(r"\s+", "", m.group(0))
    low = line.lower()
    if re.search(r"\byesterday\b", low):
        out["applied_on"] = today - timedelta(days=1)
    elif re.search(r"\btoday\b", low):
        out["applied_on"] = today
    elif m := _MDY.search(line):
        mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
        year = today.year if not y else (int(y) + 2000 if len(y) == 2 else int(y))
        try:
            when = date(year, mo, d)
            # "11/30" typed in January means last year
            out["applied_on"] = when if when <= today else date(year - 1, mo, d)
        except ValueError:
            pass
    for ch, rx in _CHANNELS:
        if rx.search(line):
            out["channel"] = ch
            break
    return out


def parse_quick_add(line: str, deps: Deps, today: date | None = None) -> QuickAddDraft:
    today = today or date.today()
    line = " ".join(line.split())[:MAX_LINE]
    if not line:
        raise ValueError("type a line first, e.g. 'applied Acme AI engineer, put 85k'")
    pre = prepass(line, today)
    res = deps.llm.structured(
        "parse_quick_add",
        prompts.PARSE_QUICK_ADD,
        f"Today: {today.isoformat()}\nLine: {line}",
        ApplicationDraftOut,
    )
    o = res.output
    company = (o.company or "").strip()
    if not company:
        raise ValueError("couldn't find a company in that line")
    applied_on = pre.get("applied_on")
    if applied_on is None and o.applied_on:
        try:
            applied_on = min(date.fromisoformat(o.applied_on), today)
        except ValueError:
            applied_on = None
    return QuickAddDraft(
        company=company,
        role=(o.role or "").strip() or None,
        channel=pre.get("channel") or o.channel or "other",
        salary_entered=pre.get("salary_entered") or o.salary_entered,
        applied_on=applied_on or today,
        notes=(o.notes or "").strip() or None,
    )
