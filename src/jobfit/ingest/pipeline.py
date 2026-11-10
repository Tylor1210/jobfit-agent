"""Daily ingestion pipeline. Called by `jobfit ingest` (and the scheduled GitHub Action).

  1. fetch every enabled source; dedupe by (source, job id); track first/last seen
  2. close jobs that left a full-snapshot board, or went stale / past deadline on search sources
  3. prefilter new open jobs with no LLM: title keywords, location, profile similarity
  4. run the fit graph on the top N unscored candidates, stopping cleanly at the cost cap

Each stage records counts into one ingest_runs row for the dashboard's Ops page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from jobfit.graphs.common import Deps
from jobfit.graphs.fit import run_fit
from jobfit.ingest.config import IngestConfig, SourceKeys, Watchlist
from jobfit.ingest.prefilter import ProfileMatcher, evaluate
from jobfit.ingest.sources import Source, build_sources, make_client
from jobfit.profile.loader import profile_version
from jobfit.rag.embedder import Embedder
from jobfit.schemas import JobPosting
from jobfit.store.ingest_store import IngestStore, SyncStats, stats_json

log = logging.getLogger("jobfit.ingest")

# Sources whose fetch is not a full snapshot; they close by deadline/staleness instead.
SEARCH_SOURCES = ("remotive", "remoteok", "usajobs", "adzuna")


@dataclass(slots=True)
class IngestReport:
    run_id: int
    status: str = "ok"
    per_source: dict[str, SyncStats] = field(default_factory=dict)
    prefilter: dict[str, int] = field(default_factory=dict)  # verdict -> count
    scored: list[tuple[str, int, str]] = field(default_factory=list)  # (job id, score, verdict)
    rescored: list[tuple[str, int, str]] = field(default_factory=list)  # against a newer profile
    spend_usd: float = 0.0
    notes: list[str] = field(default_factory=list)


def run_ingest(
    deps: Deps,
    store: IngestStore,
    cfg: IngestConfig,
    watchlist: Watchlist,
    keys: SourceKeys,
    embedder: Embedder,
    *,
    only: set[str] | None = None,
    score: bool = True,
    top_n: int | None = None,
    sources: list[Source] | None = None,
) -> IngestReport:
    run_id = store.start_run()
    rep = IngestReport(run_id)
    spent_before = deps.llm.ledger.spent_today()
    try:
        srcs, notes = (
            (sources, []) if sources is not None else build_sources(cfg, watchlist, keys, only)
        )
        rep.notes += notes

        # 1-2. fetch + sync + close
        with make_client() as client:
            for src in srcs:
                total = SyncStats()
                try:
                    for batch in src.fetch(client):
                        total.add(store.sync_batch(batch))
                    if src.name in SEARCH_SOURCES:
                        total.closed += store.close_stale(src.name, cfg.stale_days)
                except Exception as e:  # DB hiccup on one source: log it, keep going
                    log.exception("%s sync failed", src.name)
                    total.errors.append(f"sync failed: {type(e).__name__}: {str(e)[:200]}")
                rep.per_source[src.name] = total
                log.info("%s: %s", src.name, total)
        if any(s.errors for s in rep.per_source.values()):
            rep.status = "partial"

        # 3. prefilter (string checks first; only survivors get embedded)
        pending = store.unprefiltered()
        if pending:
            matcher = ProfileMatcher(deps.profile, embedder)
            for jid, v in evaluate(pending, cfg.prefilter, matcher).items():
                store.save_prefilter(jid, v.verdict, v.similarity, v.rank)
                rep.prefilter[v.verdict] = rep.prefilter.get(v.verdict, 0) + 1

        # 4. score the best new candidates, then 5. catch up jobs scored against an older
        # profile (new resume). New jobs go first: they have no score at all.
        if score:
            new = store.candidates(top_n if top_n is not None else cfg.score_top_n)
            capped = score_batch(new, deps, rep, rep.scored)
            if not capped and cfg.rescore_per_run > 0:
                stale = store.stale_scored(profile_version(), cfg.rescore_per_run)
                score_batch(stale, deps, rep, rep.rescored)
    except Exception as e:  # record the failure on the run row, then re-raise for CI to fail
        rep.status = "failed"
        rep.notes.append(f"{type(e).__name__}: {e}"[:500])
        raise
    finally:
        # GOTCHA: this bookkeeping must never raise over the error that got us here.
        try:
            rep.spend_usd = round(deps.llm.ledger.spent_today() - spent_before, 6)
        except Exception:
            log.exception("could not read spend")
        _finish(store, run_id, rep)
    return rep


def _finish(store: IngestStore, run_id: int, rep: IngestReport) -> None:
    try:
        store.finish_run(
            run_id,
            status=rep.status,
            stats=stats_json(rep.per_source),
            prefiltered=sum(rep.prefilter.values()),
            scored=len(rep.scored) + len(rep.rescored),
            spend_usd=rep.spend_usd,
            notes=[*rep.notes, f"prefilter {rep.prefilter}"],
        )
    except Exception:
        log.exception("could not record ingest run %s", run_id)


def score_batch(
    jobs: list[JobPosting], deps: Deps, rep: IngestReport, into: list[tuple[str, int, str]]
) -> bool:
    """Run the fit graph over jobs; append results to `into`. Returns True if a cost cap or the
    provider's daily quota stopped the batch (the rest stay queued for the next run)."""
    for job in jobs:
        out = run_fit(job, deps)
        err = out.get("error")
        if err and err.startswith(("cost_cap", "quota")):
            rep.status = "capped"
            why = "daily cost cap" if err.startswith("cost_cap") else "provider daily quota"
            rep.notes.append(f"stopped at {why}; remaining jobs stay queued: {err[:200]}")
            return True
        if err:
            rep.notes.append(f"{job.id}: {err}")
            continue
        fit = out["fit"]
        into.append((job.id, fit.fit_score, fit.verdict))
    return False


def run_rescore(
    deps: Deps, store: IngestStore, limit: int, include_closed: bool = False
) -> IngestReport:
    """Rescore jobs whose fit was made against an older profile. Logged as an ingest run."""
    run_id = store.start_run()
    rep = IngestReport(run_id)
    spent_before = deps.llm.ledger.spent_today()
    try:
        stale = store.stale_scored(profile_version(), limit, include_closed)
        rep.notes.append(f"rescore: {len(stale)} job(s) scored against an older profile")
        score_batch(stale, deps, rep, rep.rescored)
    finally:
        try:
            rep.spend_usd = round(deps.llm.ledger.spent_today() - spent_before, 6)
        except Exception:
            log.exception("could not read spend")
        _finish(store, run_id, rep)
    return rep


def summary(rep: IngestReport) -> dict[str, Any]:
    return {
        "run_id": rep.run_id,
        "status": rep.status,
        "sources": {
            k: {
                "fetched": v.fetched,
                "new": v.new,
                "reopened": v.reopened,
                "closed": v.closed,
                "errors": len(v.errors),
            }
            for k, v in rep.per_source.items()
        },
        "prefilter": rep.prefilter,
        "scored": len(rep.scored),
        "rescored": len(rep.rescored),
        "spend_usd": rep.spend_usd,
    }
