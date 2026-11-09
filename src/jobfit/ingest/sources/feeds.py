"""Aggregator adapters: Remotive, Remote OK, USAJOBS, Adzuna. None of these is a full snapshot,
so their jobs close by deadline or staleness, never by absence from one fetch.

Terms we follow:
- Remotive and Remote OK: keep their job URL as the link and credit them as the source
  (the dashboard shows "via Remotive" / "via Remote OK"). One fetch per day.
- USAJOBS: free API key; the User-Agent must be the email the key was issued to.
- Adzuna: free app_id/app_key. Descriptions are ~500-char snippets, so fit scores from Adzuna
  jobs are rougher; the link leads to the full posting.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from jobfit.ingest.sources.base import Batch, safe_fetch
from jobfit.ingest.text import extract_pay, format_range, html_to_text
from jobfit.schemas import JobPosting


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


# ---- Remotive --------------------------------------------------------------------------------


def remotive_job(raw: dict[str, Any], category: str) -> JobPosting:
    desc = html_to_text(raw.get("description"))
    return JobPosting(
        id=f"remotive:{raw['id']}",
        source="remotive",
        external_id=str(raw["id"]),
        board=category,
        company=raw.get("company_name") or "Unknown",
        title=raw["title"].strip(),
        location=f"Remote ({raw.get('candidate_required_location') or 'unspecified'})",
        url=raw.get("url"),
        description=desc,
        pay=(raw.get("salary") or "").strip() or extract_pay(desc),
        remote=True,
        posted_at=_dt(raw.get("publication_date")),
    )


class Remotive:
    name = "remotive"

    def __init__(self, categories: list[str]) -> None:
        self.categories = categories

    def fetch(self, client: httpx.Client) -> list[Batch]:
        def one(cat: str) -> Batch:
            r = client.get("https://remotive.com/api/remote-jobs", params={"category": cat})
            r.raise_for_status()
            return Batch(self.name, cat, [remotive_job(j, cat) for j in r.json()["jobs"]])

        return [safe_fetch(self.name, c, lambda c=c: one(c)) for c in self.categories]


# ---- Remote OK -------------------------------------------------------------------------------


def remoteok_job(raw: dict[str, Any]) -> JobPosting:
    desc = html_to_text(raw.get("description"))
    lo, hi = raw.get("salary_min") or None, raw.get("salary_max") or None
    return JobPosting(
        id=f"remoteok:{raw['id']}",
        source="remoteok",
        external_id=str(raw["id"]),
        board="feed",
        company=raw.get("company") or "Unknown",
        title=(raw.get("position") or "").strip(),
        location=f"Remote ({raw.get('location') or 'unspecified'})",
        url=raw.get("url"),
        description=desc,
        pay=format_range(lo, hi, "/yr") or extract_pay(desc),
        remote=True,
        posted_at=_dt(raw.get("date")),
    )


class RemoteOK:
    name = "remoteok"

    def fetch(self, client: httpx.Client) -> list[Batch]:
        def one() -> Batch:
            r = client.get("https://remoteok.com/api")
            r.raise_for_status()
            # GOTCHA: element 0 is the legal notice, not a job.
            items = [j for j in r.json() if isinstance(j, dict) and j.get("id")]
            return Batch(self.name, "feed", [remoteok_job(j) for j in items if j.get("position")])

        return [safe_fetch(self.name, "feed", one)]


# ---- USAJOBS ---------------------------------------------------------------------------------


def usajobs_job(item: dict[str, Any], keyword: str) -> JobPosting:
    d = item["MatchedObjectDescriptor"]
    details = (d.get("UserArea") or {}).get("Details") or {}
    desc = "\n\n".join(
        x
        for x in [
            details.get("JobSummary"),
            d.get("QualificationSummary"),
            "\n".join(f"- {m}" for m in details.get("MajorDuties") or []),
        ]
        if x
    )
    rem = (d.get("PositionRemuneration") or [{}])[0]
    unit = {"PA": "/yr", "PH": "/hr"}.get(rem.get("RateIntervalCode", ""), "")
    lo = float(rem["MinimumRange"]) if rem.get("MinimumRange") else None
    hi = float(rem["MaximumRange"]) if rem.get("MaximumRange") else None
    return JobPosting(
        id=f"usajobs:{d['PositionID']}",
        source="usajobs",
        external_id=str(d["PositionID"]),
        board=keyword,
        company=d.get("OrganizationName") or d.get("DepartmentName") or "USAJOBS",
        title=d["PositionTitle"].strip(),
        location=d.get("PositionLocationDisplay"),
        url=d.get("PositionURI"),
        description=html_to_text(desc),
        pay=format_range(lo, hi, unit),
        remote=bool(details.get("RemoteIndicator"))
        or "remote" in (d.get("PositionLocationDisplay") or "").lower(),
        posted_at=_dt(d.get("PublicationStartDate")),
        closes_at=_dt(d.get("ApplicationCloseDate")),
    )


class USAJobs:
    name = "usajobs"

    def __init__(self, keywords: list[str], per_keyword: int, api_key: str, email: str) -> None:
        self.keywords, self.per_keyword = keywords, per_keyword
        self.headers = {
            "Host": "data.usajobs.gov",
            "User-Agent": email,
            "Authorization-Key": api_key,
        }

    def fetch(self, client: httpx.Client) -> list[Batch]:
        def one(kw: str) -> Batch:
            r = client.get(
                "https://data.usajobs.gov/api/search",
                headers=self.headers,
                params={"Keyword": kw, "ResultsPerPage": self.per_keyword},
            )
            r.raise_for_status()
            items = r.json()["SearchResult"]["SearchResultItems"]
            return Batch(self.name, kw, [usajobs_job(i, kw) for i in items])

        return [safe_fetch(self.name, k, lambda k=k: one(k)) for k in self.keywords]


# ---- Adzuna ----------------------------------------------------------------------------------


def adzuna_job(raw: dict[str, Any], query: str) -> JobPosting:
    loc = (raw.get("location") or {}).get("display_name")
    title = html_to_text(raw.get("title"))
    desc = html_to_text(raw.get("description"))
    return JobPosting(
        id=f"adzuna:{raw['id']}",
        source="adzuna",
        external_id=str(raw["id"]),
        board=query,
        company=(raw.get("company") or {}).get("display_name") or "Unknown",
        title=title,
        location=loc,
        url=raw.get("redirect_url"),
        description=desc,
        pay=format_range(raw.get("salary_min"), raw.get("salary_max"), "/yr"),
        remote="remote" in f"{title} {desc} {loc}".lower(),
        posted_at=_dt(raw.get("created")),
    )


class Adzuna:
    name = "adzuna"

    def __init__(
        self, country: str, queries: list[dict[str, str]], per_query: int, app_id: str, app_key: str
    ) -> None:
        self.country, self.queries, self.per_query = country, queries, per_query
        self.auth = {"app_id": app_id, "app_key": app_key}

    def fetch(self, client: httpx.Client) -> list[Batch]:
        def one(q: dict[str, str]) -> Batch:
            label = f"{q['what']} @ {q.get('where') or 'anywhere'}"
            params: dict[str, str | int] = {
                **self.auth,
                "what": q["what"],
                "results_per_page": self.per_query,
                "content-type": "application/json",
            }
            if q.get("where"):
                params["where"] = q["where"]
            r = client.get(
                f"https://api.adzuna.com/v1/api/jobs/{self.country}/search/1", params=params
            )
            r.raise_for_status()
            return Batch(self.name, label, [adzuna_job(j, label) for j in r.json()["results"]])

        return [safe_fetch(self.name, q["what"], lambda q=q: one(q)) for q in self.queries]
