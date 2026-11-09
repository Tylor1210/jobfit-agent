"""Company job-board adapters: Greenhouse, Lever, Ashby. Each board fetch is a full snapshot of
that company's open jobs, so a job missing from it has closed.

Endpoints (public, no key):
  https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
  https://api.lever.co/v0/postings/{token}?mode=json
  https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from jobfit.ingest.config import Board
from jobfit.ingest.sources.base import Batch, safe_fetch
from jobfit.ingest.text import extract_pay, format_range, html_to_text
from jobfit.schemas import JobPosting


def _dt(v: Any) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, int | float) or str(v).isdigit():  # Lever: epoch milliseconds
        return datetime.fromtimestamp(int(v) / 1000, UTC)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _is_remote(*texts: str | None) -> bool:
    return any(t and "remote" in t.lower() for t in texts)


# ---- Greenhouse ------------------------------------------------------------------------------


def greenhouse_job(raw: dict[str, Any], board: Board) -> JobPosting:
    desc = html_to_text(raw.get("content"))
    loc = (raw.get("location") or {}).get("name")
    return JobPosting(
        id=f"greenhouse:{raw['id']}",
        source="greenhouse",
        external_id=str(raw["id"]),
        board=board.token,
        company=board.name or raw.get("company_name") or board.token,
        title=raw["title"].strip(),
        location=loc,
        url=raw.get("absolute_url"),
        description=desc,
        pay=extract_pay(desc),
        remote=_is_remote(loc, raw["title"]),
        posted_at=_dt(raw.get("first_published") or raw.get("updated_at")),
    )


class Greenhouse:
    name = "greenhouse"

    def __init__(self, boards: list[Board]) -> None:
        self.boards = boards

    def fetch(self, client: httpx.Client) -> list[Batch]:
        def one(b: Board) -> Batch:
            r = client.get(
                f"https://boards-api.greenhouse.io/v1/boards/{b.token}/jobs",
                params={"content": "true"},
            )
            r.raise_for_status()
            jobs = [greenhouse_job(j, b) for j in r.json()["jobs"]]
            return Batch(self.name, b.token, jobs, full_snapshot=True)

        return [safe_fetch(self.name, b.token, lambda b=b: one(b)) for b in self.boards]


# ---- Lever -----------------------------------------------------------------------------------


def lever_job(raw: dict[str, Any], board: Board) -> JobPosting:
    parts = [raw.get("descriptionPlain") or html_to_text(raw.get("description"))]
    for lst in raw.get("lists") or []:
        parts.append(f"{lst.get('text', '').strip()}\n{html_to_text(lst.get('content'))}")
    parts.append(raw.get("additionalPlain") or "")
    desc = "\n\n".join(p.strip() for p in parts if p and p.strip())
    cats = raw.get("categories") or {}
    loc = cats.get("location") or ", ".join(cats.get("allLocations") or []) or None
    if loc and raw.get("country") and raw["country"] not in loc:
        loc = f"{loc} ({raw['country']})"
    sal = raw.get("salaryRange") or {}
    unit = {"per-hour-wage": "/hr", "per-year-salary": "/yr"}.get(sal.get("interval", ""), "")
    pay = format_range(sal.get("min"), sal.get("max"), unit, sal.get("currency") or "USD")
    return JobPosting(
        id=f"lever:{raw['id']}",
        source="lever",
        external_id=raw["id"],
        board=board.token,
        company=board.name or board.token,
        title=raw["text"].strip(),
        location=loc,
        url=raw.get("hostedUrl"),
        description=desc,
        pay=pay or extract_pay(desc),
        remote=raw.get("workplaceType") == "remote" or _is_remote(loc),
        posted_at=_dt(raw.get("createdAt")),
    )


class Lever:
    name = "lever"

    def __init__(self, boards: list[Board]) -> None:
        self.boards = boards

    def fetch(self, client: httpx.Client) -> list[Batch]:
        def one(b: Board) -> Batch:
            r = client.get(f"https://api.lever.co/v0/postings/{b.token}", params={"mode": "json"})
            r.raise_for_status()
            return Batch(
                self.name, b.token, [lever_job(j, b) for j in r.json()], full_snapshot=True
            )

        return [safe_fetch(self.name, b.token, lambda b=b: one(b)) for b in self.boards]


# ---- Ashby -----------------------------------------------------------------------------------


def ashby_job(raw: dict[str, Any], board: Board) -> JobPosting:
    desc = raw.get("descriptionPlain") or html_to_text(raw.get("descriptionHtml"))
    locs = [raw.get("location")] + [s.get("location") for s in raw.get("secondaryLocations") or []]
    loc = " / ".join(x for x in locs if x) or None
    country = ((raw.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
    if loc and country and country not in loc:
        loc = f"{loc} ({country})"
    comp = raw.get("compensation") or {}
    pay = comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary")
    return JobPosting(
        id=f"ashby:{raw['id']}",
        source="ashby",
        external_id=raw["id"],
        board=board.token,
        company=board.name or board.token,
        title=raw["title"].strip(),
        location=loc,
        url=raw.get("jobUrl"),
        description=desc,
        pay=(re.sub(r"\s*[\u2013\u2014-]\s*", " to ", pay) if pay else None) or extract_pay(desc),
        remote=bool(raw.get("isRemote")) or raw.get("workplaceType") == "Remote",
        posted_at=_dt(raw.get("publishedAt")),
    )


class Ashby:
    name = "ashby"

    def __init__(self, boards: list[Board]) -> None:
        self.boards = boards

    def fetch(self, client: httpx.Client) -> list[Batch]:
        def one(b: Board) -> Batch:
            r = client.get(
                f"https://api.ashbyhq.com/posting-api/job-board/{b.token}",
                params={"includeCompensation": "true"},
            )
            r.raise_for_status()
            jobs = [ashby_job(j, b) for j in r.json()["jobs"] if j.get("isListed", True)]
            return Batch(self.name, b.token, jobs, full_snapshot=True)

        return [safe_fetch(self.name, b.token, lambda b=b: one(b)) for b in self.boards]
