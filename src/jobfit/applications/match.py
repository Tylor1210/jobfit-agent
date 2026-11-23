"""Fuzzy matching of an email (company + role) to an application, and of an application to a
job posting. Pure code, no LLM. Used by the mail graph and the manual/quick-add API.

CONSTRAINT: never guess a role. With no role in the email, a company with exactly one
application is a match; two or more (or zero) is an ambiguous result the graph flags for review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from rapidfuzz import fuzz

from jobfit.applications.models import Application

COMPANY_MIN = 90
ROLE_MIN = 80

_SUFFIX = re.compile(
    r"\b(inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|gmbh|plc|pbc|"
    r"technologies|technology|labs|group|holdings|the|hq|careers|talent|recruiting|team)\b"
)
_ROLE_ABBR = {
    r"\bsr\b": "senior",
    r"\bjr\b": "junior",
    r"\beng\b": "engineer",
    r"\bswe\b": "software engineer",
    r"\bml\b": "machine learning",
    r"\bfde\b": "forward deployed engineer",
    r"\bmgr\b": "manager",
    r"\bdev\b": "developer",
}
_ROLE_NOISE = re.compile(r"\((remote|hybrid|onsite|on-site|us|usa)[^)]*\)|\b(remote|hybrid)\b")


def norm_company(name: str) -> str:
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = _SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_role(title: str) -> str:
    s = _ROLE_NOISE.sub(" ", title.lower())
    s = re.sub(r"[^\w\s]", " ", s)
    for rx, rep in _ROLE_ABBR.items():
        s = re.sub(rx, rep, s)
    return re.sub(r"\s+", " ", s).strip()


def company_score(a: str, b: str) -> float:
    x, y = norm_company(a), norm_company(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 100.0
    # "Acme" vs "Acme Robotics": the shorter name is the other's leading words. WHY not
    # token_set_ratio: it scores "ai" vs "scale ai" as 100, which merges unrelated companies.
    short, long_ = sorted((x, y), key=len)
    if len(short) >= 4 and (long_.startswith(short + " ") or long_.replace(" ", "") == short):
        return 95.0
    return float(max(fuzz.ratio(x, y), fuzz.token_sort_ratio(x, y)))


_GENERIC = frozenset(
    [
        "engineer",
        "engineering",
        "developer",
        "senior",
        "junior",
        "lead",
        "staff",
        "principal",
        "i",
        "ii",
        "iii",
        "iv",
        "associate",
        "intern",
        "specialist",
        "manager",
        "of",
        "and",
        "the",
        "for",
    ]
)


def _core(s: str) -> str:
    return " ".join(t for t in s.split() if t not in _GENERIC)


def _sim(x: str, y: str) -> float:
    return float(max(fuzz.token_sort_ratio(x, y), fuzz.token_set_ratio(x, y) - 5))


def role_score(a: str, b: str) -> float:
    x, y = norm_role(a), norm_role(b)
    if not x or not y:
        return 0.0
    full = _sim(x, y)
    cx, cy = _core(x), _core(y)
    # WHY the core check: "ai engineer" vs "data engineer" scores ~83 on the full strings
    # because "engineer" dominates. The distinguishing words have to agree too.
    return min(full, _sim(cx, cy)) if cx and cy else full


MatchKind = Literal["matched", "company_only", "none", "ambiguous"]


@dataclass
class MatchResult:
    kind: MatchKind
    app: Application | None = None
    candidates: list[Application] = field(default_factory=list)


def match_application(
    company: str | None, role: str | None, apps: list[Application]
) -> MatchResult:
    if not company:
        return MatchResult("ambiguous")
    same = [a for a in apps if company_score(company, a.company) >= COMPANY_MIN]
    if not same:
        return MatchResult("none")
    if role:
        scored = sorted(
            ((role_score(role, a.role), a) for a in same if a.role),
            key=lambda t: (t[0], t[1].last_activity_at),
            reverse=True,
        )
        if scored and scored[0][0] >= ROLE_MIN:
            return MatchResult("matched", scored[0][1], same)
        # The only application here never had a role (e.g. created from a vague rejection):
        # this email names it, so link it and let the caller fill the role in.
        roleless = [a for a in same if not a.role]
        if len(same) == 1 and roleless:
            return MatchResult("matched", roleless[0], same)
        return MatchResult("none", None, same)  # a different role at a known company
    if len(same) == 1:
        return MatchResult("company_only", same[0], same)
    return MatchResult("ambiguous", None, same)


def match_job(company: str, role: str | None, jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best posting for an application, or None. Needs both company and role to agree."""
    if not role:
        return None
    best: tuple[float, dict[str, Any]] | None = None
    for j in jobs:
        if company_score(company, j["company"]) < COMPANY_MIN:
            continue
        s = role_score(role, j["title"])
        if s >= ROLE_MIN and (best is None or s > best[0]):
            best = (s, j)
    return best[1] if best else None
