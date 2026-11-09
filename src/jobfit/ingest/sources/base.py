"""Source adapter interface. Each adapter turns one public job API into JobPostings.

A source returns one `Batch` per board/feed/query it fetched. The batch says whether it is a
full snapshot (every open job on that board is in it, so anything missing has closed) or a
search result (missing means nothing; jobs close by date or staleness instead).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from jobfit.schemas import JobPosting

USER_AGENT = "jobfit-agent/0.1 (personal job search tool)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass(slots=True)
class Batch:
    source: str
    board: str  # watchlist token, feed name, or search query
    jobs: list[JobPosting] = field(default_factory=list)
    full_snapshot: bool = False
    error: str | None = None  # set when the fetch failed; such batches never close jobs


class Source(Protocol):
    name: str

    def fetch(self, client: httpx.Client) -> list[Batch]: ...


def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        transport=httpx.HTTPTransport(retries=2),  # connection-level retries only
        follow_redirects=True,
    )


def safe_fetch(source: str, board: str, fn: object) -> Batch:
    """Run one board fetch; any failure becomes Batch.error instead of killing the whole run."""
    try:
        batch: Batch = fn()  # type: ignore[operator]
        return batch
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        # CONSTRAINT: errors are stored and shown on the Ops page; strip query strings so API
        # keys passed as params (Adzuna app_key) never leak into them.
        msg = re.sub(r"\?[^\s'\"]*", "?…", f"{type(e).__name__}: {e}")
        return Batch(source, board, error=msg[:300])
