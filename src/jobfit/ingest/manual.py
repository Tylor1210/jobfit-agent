"""Manual job entry: a pasted URL or pasted posting text. Used by `jobfit add` and the
dashboard's Add job page (Phase 5).

CONSTRAINT: LinkedIn and Indeed URLs are never fetched (their terms forbid automated access).
The caller gets a clear error and pastes the posting text instead.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

import httpx

from jobfit.ingest.config import Board
from jobfit.ingest.sources.base import make_client
from jobfit.ingest.sources.boards import ashby_job, greenhouse_job, lever_job
from jobfit.ingest.text import extract_pay, html_to_text
from jobfit.schemas import JobPosting

BLOCKED_HOSTS = ("linkedin.com", "indeed.com")


class ManualAddError(ValueError):
    """Shown to the user as-is."""


def from_text(
    text: str, company: str, title: str, url: str | None = None, location: str | None = None
) -> JobPosting:
    if len(text.strip()) < 200:
        raise ManualAddError("posting text is too short; paste the full description")
    ext = hashlib.sha256(f"{company}|{title}|{text[:500]}".encode()).hexdigest()[:12]
    return JobPosting(
        id=f"manual:{ext}",
        source="manual",
        external_id=ext,
        board="manual",
        company=company,
        title=title,
        location=location,
        url=url,
        description=text.strip(),
        pay=extract_pay(text),
        remote="remote" in f"{location or ''} {title}".lower(),
    )


def from_url(url: str, client: httpx.Client | None = None) -> JobPosting:
    host = (urlparse(url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in BLOCKED_HOSTS):
        raise ManualAddError(
            f"{host} postings can't be fetched automatically (their terms "
            "forbid it). Paste the posting text instead."
        )
    own = client is None
    c = client or make_client()
    try:
        path = urlparse(url).path.strip("/").split("/")
        if host.endswith("greenhouse.io") and "jobs" in path:
            token, jid = path[path.index("jobs") - 1], path[path.index("jobs") + 1]
            r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{jid}")
            r.raise_for_status()
            return greenhouse_job(r.json(), Board(token=token))
        if host == "jobs.lever.co" and len(path) >= 2:
            r = c.get(f"https://api.lever.co/v0/postings/{path[0]}/{path[1]}")
            r.raise_for_status()
            return lever_job(r.json(), Board(token=path[0]))
        if host == "jobs.ashbyhq.com" and len(path) >= 2:
            r = c.get(f"https://api.ashbyhq.com/posting-api/job-board/{path[0]}")
            r.raise_for_status()
            for j in r.json()["jobs"]:
                if j["id"] == path[1]:
                    return ashby_job(j, Board(token=path[0]))
            raise ManualAddError("that Ashby posting is no longer listed")
        # Anything else: fetch the page once (the user asked for this URL) and keep its text.
        r = c.get(url, headers={"Accept": "text/html"})
        r.raise_for_status()
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.I | re.S)
        title = html_to_text(m.group(1)) if m else "Untitled role"
        text = html_to_text(r.text)
        job = from_text(
            text,
            company=host.removeprefix("www.").split(".")[0].title(),
            title=title[:120],
            url=url,
        )
        return job.model_copy(update={"board": "manual-url"})
    except httpx.HTTPStatusError as e:
        raise ManualAddError(f"could not fetch that posting (HTTP {e.response.status_code})") from e
    except httpx.HTTPError as e:
        raise ManualAddError(f"could not reach {host}: {type(e).__name__}") from e
    finally:
        if own:
            c.close()
