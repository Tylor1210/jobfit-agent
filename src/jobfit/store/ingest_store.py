"""Persistence for daily ingestion: sync fetched batches, close jobs, prefilter bookkeeping,
candidate selection, and the run log. Used by jobfit.ingest.pipeline.

Closing rules (the reason this module exists: expired postings waste applications):
- full-snapshot batch (a company board): open jobs on that board missing from the batch close now
- search batch (USAJOBS, Adzuna, feeds): close when closes_at has passed, or unseen for stale_days
- a failed fetch never closes anything
- a board that suddenly returns 0 jobs while we hold >= EMPTY_GUARD open ones is treated as a
  glitch and closes nothing (logged), since "everything closed at once" is rarely true
- closing moves the card to the Closed column only from New/Shortlisted; Applied and later keep
  their stage so Tylor's own tracking is never overwritten. A job that reappears reopens.
"""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from jobfit.ingest.sources.base import Batch
from jobfit.schemas import JobPosting
from jobfit.store.repo import InMemoryRepo

if TYPE_CHECKING:
    import psycopg

EMPTY_GUARD = 3
_AUTO_CLOSE_STAGES = ("new", "shortlisted")


@dataclass(slots=True)
class SyncStats:
    fetched: int = 0
    new: int = 0
    reopened: int = 0
    closed: int = 0
    errors: list[str] = field(default_factory=list)

    def add(self, other: SyncStats) -> None:
        self.fetched += other.fetched
        self.new += other.new
        self.reopened += other.reopened
        self.closed += other.closed
        self.errors += other.errors


class IngestStore(Protocol):
    def sync_batch(self, b: Batch) -> SyncStats: ...
    def close_stale(self, source: str, stale_days: int) -> int: ...
    def unprefiltered(self) -> list[JobPosting]: ...
    def save_prefilter(
        self, job_id: str, verdict: str, similarity: float | None, rank: float | None
    ) -> None: ...
    def candidates(self, n: int) -> list[JobPosting]: ...
    def stale_scored(
        self, version: str, n: int, include_closed: bool = False
    ) -> list[JobPosting]: ...
    def start_run(self) -> int: ...
    def finish_run(self, run_id: int, **fields: Any) -> None: ...


# ---- in-memory (tests, zero-setup CLI) --------------------------------------------------------


class InMemoryIngestStore:
    def __init__(self, repo: InMemoryRepo) -> None:
        self.repo = repo
        self.meta: dict[str, dict[str, Any]] = {}  # job id -> prefilter/similarity/seen
        self.runs: dict[int, dict[str, Any]] = {}

    def _close(self, jid: str) -> None:
        j = self.repo.jobs[jid]
        stage = "closed" if j.stage in _AUTO_CLOSE_STAGES else j.stage
        self.repo.jobs[jid] = j.model_copy(update={"status": "closed", "stage": stage})

    def sync_batch(self, b: Batch) -> SyncStats:
        st = SyncStats(fetched=len(b.jobs))
        if b.error:
            st.errors.append(f"{b.board}: {b.error}")
            return st
        now = datetime.now(UTC)
        seen: set[str] = set()
        for job in b.jobs:
            seen.add(job.id)
            old = self.repo.jobs.get(job.id)
            if old is None:
                st.new += 1
                self.repo.jobs[job.id] = job.model_copy(
                    update={"first_seen": now, "last_seen": now}
                )
                continue
            upd: dict[str, Any] = {
                k: getattr(job, k)
                for k in ("title", "location", "url", "description", "pay", "remote", "closes_at")
            }
            upd["last_seen"] = now
            if old.status == "closed":
                st.reopened += 1
                upd["status"] = "open"
                if old.stage == "closed":
                    upd["stage"] = "new"
            self.repo.jobs[job.id] = old.model_copy(update=upd)
        if b.full_snapshot:
            gone = [
                j.id
                for j in self.repo.jobs.values()
                if j.source == b.source
                and j.board == b.board
                and j.status == "open"
                and j.id not in seen
            ]
            if not b.jobs and len(gone) >= EMPTY_GUARD:
                st.errors.append(f"{b.board}: returned 0 jobs, kept {len(gone)} open (guard)")
                return st
            for jid in gone:
                self._close(jid)
            st.closed = len(gone)
        return st

    def close_stale(self, source: str, stale_days: int) -> int:
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=stale_days)
        stale = [
            j.id
            for j in self.repo.jobs.values()
            if j.source == source
            and j.status == "open"
            and ((j.closes_at and j.closes_at < now) or (j.last_seen and j.last_seen < cutoff))
        ]
        for jid in stale:
            self._close(jid)
        return len(stale)

    def unprefiltered(self) -> list[JobPosting]:
        return [
            j
            for j in self.repo.jobs.values()
            if j.status == "open" and "prefilter" not in self.meta.get(j.id, {})
        ]

    def save_prefilter(
        self, job_id: str, verdict: str, similarity: float | None, rank: float | None
    ) -> None:
        self.meta.setdefault(job_id, {}).update(prefilter=verdict, similarity=similarity, rank=rank)

    def candidates(self, n: int) -> list[JobPosting]:
        pool = [
            j
            for j in self.repo.jobs.values()
            if j.status == "open"
            and j.id not in self.repo.fits
            and self.meta.get(j.id, {}).get("prefilter") == "pass"
        ]
        pool.sort(key=lambda j: self.meta[j.id].get("rank") or 0.0, reverse=True)
        return pool[:n]

    def stale_scored(self, version: str, n: int, include_closed: bool = False) -> list[JobPosting]:
        pool = [
            j
            for j in self.repo.jobs.values()
            if j.id in self.repo.fits
            and self.repo.fits[j.id].profile_version != version
            and (include_closed or j.status == "open")
        ]
        pool.sort(key=lambda j: self.repo.fits[j.id].fit_score, reverse=True)
        return pool[:n]

    def start_run(self) -> int:
        rid = len(self.runs) + 1
        self.runs[rid] = {"status": "running"}
        return rid

    def finish_run(self, run_id: int, **fields: Any) -> None:
        self.runs[run_id].update(fields)


# ---- Postgres ---------------------------------------------------------------------------------

_JOB_COLS = (
    "id",
    "source",
    "external_id",
    "board",
    "company",
    "title",
    "location",
    "url",
    "description",
    "pay",
    "posted_at",
    "remote",
    "closes_at",
)


class PgIngestStore:
    def __init__(self, dsn: str) -> None:
        from jobfit.store.db import SharedConnection

        self._db = SharedConnection(dsn, dict_rows=True)

    def _conn(self) -> AbstractContextManager[psycopg.Connection[Any]]:
        # Shared, timeout-bounded connection; `with` does not close it (see store/db.py).
        return self._db.use()

    def sync_batch(self, b: Batch) -> SyncStats:
        st = SyncStats(fetched=len(b.jobs))
        if b.error:
            st.errors.append(f"{b.board}: {b.error}")
            return st
        cols = ", ".join(_JOB_COLS)
        vals = ", ".join(f"%({c})s" for c in _JOB_COLS)
        with self._conn() as c, c.transaction():
            for job in b.jobs:
                row = c.execute(
                    f"""insert into jobs ({cols}) values ({vals})
                        on conflict (id) do update set
                          title = excluded.title, location = excluded.location,
                          url = excluded.url, description = excluded.description,
                          pay = excluded.pay, remote = excluded.remote,
                          closes_at = excluded.closes_at, board = excluded.board,
                          last_seen = now(), status = 'open', closed_at = null,
                          stage = case when jobs.status = 'closed' and jobs.stage = 'closed'
                                  then 'new' else jobs.stage end
                        returning (xmax = 0) as inserted,
                          (select status from jobs j2 where j2.id = jobs.id) as prev_status""",
                    job.model_dump(include=set(_JOB_COLS)),
                ).fetchone()
                # GOTCHA: xmax = 0 is Postgres' tell for "this row was inserted, not updated".
                if row and row["inserted"]:
                    st.new += 1
                elif row and row["prev_status"] == "closed":
                    st.reopened += 1
            if b.full_snapshot:
                ids = [j.id for j in b.jobs]
                n_gone = c.execute(
                    """select count(*) as n from jobs where source = %s and board = %s
                       and status = 'open' and not (id = any(%s))""",
                    (b.source, b.board, ids),
                ).fetchone()
                gone = n_gone["n"] if n_gone else 0
                if not b.jobs and gone >= EMPTY_GUARD:
                    st.errors.append(f"{b.board}: returned 0 jobs, kept {gone} open (guard)")
                    return st
                st.closed = c.execute(
                    """update jobs set status = 'closed', closed_at = now(),
                         stage = case when stage in ('new','shortlisted') then 'closed'
                                 else stage end
                       where source = %s and board = %s and status = 'open'
                         and not (id = any(%s))""",
                    (b.source, b.board, ids),
                ).rowcount
        return st

    def close_stale(self, source: str, stale_days: int) -> int:
        with self._conn() as c:
            return c.execute(
                """update jobs set status = 'closed', closed_at = now(),
                     stage = case when stage in ('new','shortlisted') then 'closed' else stage end
                   where source = %s and status = 'open'
                     and (closes_at < now() or last_seen < now() - make_interval(days => %s))""",
                (source, stale_days),
            ).rowcount

    def unprefiltered(self) -> list[JobPosting]:
        with self._conn() as c:
            rows = c.execute(
                "select * from jobs where status = 'open' and prefilter is null"
            ).fetchall()
        return [JobPosting.model_validate(r) for r in rows]

    def save_prefilter(
        self, job_id: str, verdict: str, similarity: float | None, rank: float | None
    ) -> None:
        with self._conn() as c:
            c.execute(
                "update jobs set prefilter = %s, similarity = %s, rank = %s where id = %s",
                (verdict, similarity, rank, job_id),
            )

    def candidates(self, n: int) -> list[JobPosting]:
        with self._conn() as c:
            rows = c.execute(
                """select * from jobs where status = 'open' and prefilter = 'pass'
                   and fit is null order by rank desc nulls last limit %s""",
                (n,),
            ).fetchall()
        return [JobPosting.model_validate(r) for r in rows]

    def stale_scored(self, version: str, n: int, include_closed: bool = False) -> list[JobPosting]:
        # Best-scoring first: if the quota runs out midway, the jobs that matter most are fresh.
        with self._conn() as c:
            rows = c.execute(
                """select * from jobs where fit is not null
                   and fit->>'profile_version' is distinct from %s
                   and (%s or status = 'open')
                   order by fit_score desc nulls last limit %s""",
                (version, include_closed, n),
            ).fetchall()
        return [JobPosting.model_validate(r) for r in rows]

    def start_run(self) -> int:
        with self._conn() as c:
            row = c.execute("insert into ingest_runs default values returning id").fetchone()
        assert row is not None
        return int(row["id"])

    def finish_run(self, run_id: int, **fields: Any) -> None:
        allowed = {"status", "stats", "prefiltered", "scored", "spend_usd", "notes"}
        # CONSTRAINT: column names are interpolated, so only known columns pass.
        if set(fields) - allowed:
            raise ValueError(f"unknown ingest_runs columns: {set(fields) - allowed}")
        data = {k: json.dumps(v) if k in ("stats", "notes") else v for k, v in fields.items()}
        sets = ", ".join(f"{k} = %({k})s" for k in data)
        with self._conn() as c:
            c.execute(
                f"update ingest_runs set {sets}, finished_at = now() where id = %(rid)s",
                {**data, "rid": run_id},
            )


def stats_json(stats: dict[str, SyncStats]) -> dict[str, Any]:
    return {k: asdict(v) for k, v in stats.items()}
