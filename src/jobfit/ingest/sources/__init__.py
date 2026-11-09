"""Build the enabled source adapters from config/ingest.yaml, config/watchlist.yaml and keys."""

from __future__ import annotations

from jobfit.ingest.config import IngestConfig, SourceKeys, Watchlist
from jobfit.ingest.sources.base import Batch, Source, make_client
from jobfit.ingest.sources.boards import Ashby, Greenhouse, Lever
from jobfit.ingest.sources.feeds import Adzuna, RemoteOK, Remotive, USAJobs

__all__ = ["Batch", "Source", "build_sources", "make_client"]


def build_sources(
    cfg: IngestConfig, wl: Watchlist, keys: SourceKeys, only: set[str] | None = None
) -> tuple[list[Source], list[str]]:
    """Return (sources to run, human-readable notes about skipped ones)."""
    out: list[Source] = []
    notes: list[str] = []

    def want(name: str) -> bool:
        return cfg.enabled(name) and (only is None or name in only)

    if want("greenhouse") and wl.greenhouse:
        out.append(Greenhouse(wl.greenhouse))
    if want("lever") and wl.lever:
        out.append(Lever(wl.lever))
    if want("ashby") and wl.ashby:
        out.append(Ashby(wl.ashby))
    if want("remotive"):
        out.append(Remotive(cfg.opts("remotive").get("categories", ["software-dev"])))
    if want("remoteok"):
        out.append(RemoteOK())
    if want("usajobs"):
        o = cfg.opts("usajobs")
        if keys.usajobs_api_key and keys.usajobs_email:
            out.append(
                USAJobs(
                    o.get("keywords", []),
                    int(o.get("results_per_keyword", 100)),
                    keys.usajobs_api_key,
                    keys.usajobs_email,
                )
            )
        else:
            notes.append("usajobs skipped: set USAJOBS_API_KEY and USAJOBS_EMAIL")
    if want("adzuna"):
        o = cfg.opts("adzuna")
        if keys.adzuna_app_id and keys.adzuna_app_key:
            out.append(
                Adzuna(
                    o.get("country", "us"),
                    o.get("queries", []),
                    int(o.get("results_per_query", 50)),
                    keys.adzuna_app_id,
                    keys.adzuna_app_key,
                )
            )
        else:
            notes.append("adzuna skipped: set ADZUNA_APP_ID and ADZUNA_APP_KEY")
    return out, notes
