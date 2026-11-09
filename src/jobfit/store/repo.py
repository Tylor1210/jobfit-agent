"""Job tracker persistence. Graph `save` and `load_job` nodes, ingestion (Phase 3) and the API
(Phase 5) all go through this interface.

TODO (Phase 7): wrap behind a TrackerSink interface with SupabaseSink default + SmartsheetSink stub.
"""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, Protocol

from jobfit.schemas import Drafts, FitResult, JobPosting, Stage

if TYPE_CHECKING:
    import psycopg


class Repo(Protocol):
    def upsert_job(self, job: JobPosting) -> None: ...
    def get_job(self, job_id: str) -> JobPosting | None: ...
    def save_fit(self, job_id: str, fit: FitResult) -> None: ...
    def get_fit(self, job_id: str) -> FitResult | None: ...
    def set_stage(self, job_id: str, stage: Stage) -> None: ...
    def save_tailor(self, job_id: str, thread_id: str, status: str, **fields: Any) -> None: ...


class InMemoryRepo:
    def __init__(self) -> None:
        self.jobs: dict[str, JobPosting] = {}
        self.fits: dict[str, FitResult] = {}
        self.tailor: dict[str, dict[str, Any]] = {}

    def upsert_job(self, job: JobPosting) -> None:
        self.jobs[job.id] = job

    def get_job(self, job_id: str) -> JobPosting | None:
        return self.jobs.get(job_id)

    def save_fit(self, job_id: str, fit: FitResult) -> None:
        self.fits[job_id] = fit

    def get_fit(self, job_id: str) -> FitResult | None:
        return self.fits.get(job_id)

    def set_stage(self, job_id: str, stage: Stage) -> None:
        self.jobs[job_id] = self.jobs[job_id].model_copy(update={"stage": stage})

    def save_tailor(self, job_id: str, thread_id: str, status: str, **fields: Any) -> None:
        self.tailor.setdefault(job_id, {}).update(thread_id=thread_id, status=status, **fields)


_TAILOR_COLS = {"drafts", "issues", "resume_pdf", "cover_pdf", "trace_url"}


class PgRepo:
    def __init__(self, dsn: str) -> None:
        from jobfit.store.db import SharedConnection

        self._db = SharedConnection(dsn, dict_rows=True)

    def _conn(self) -> AbstractContextManager[psycopg.Connection[Any]]:
        # Shared, timeout-bounded connection; `with` does not close it (see store/db.py).
        return self._db.use()

    def upsert_job(self, job: JobPosting) -> None:
        with self._conn() as c:
            c.execute(
                """insert into jobs (id, source, external_id, board, company, title, location,
                     url, description, pay, posted_at, remote, closes_at, status, stage)
                   values (%(id)s, %(source)s, %(external_id)s, %(board)s, %(company)s,
                     %(title)s, %(location)s, %(url)s, %(description)s, %(pay)s, %(posted_at)s,
                     %(remote)s, %(closes_at)s, %(status)s, %(stage)s)
                   on conflict (id) do update set title = excluded.title,
                     location = excluded.location, url = excluded.url,
                     description = excluded.description, pay = excluded.pay,
                     remote = excluded.remote, closes_at = excluded.closes_at,
                     status = excluded.status, last_seen = now()""",
                job.model_dump(),
            )

    def get_job(self, job_id: str) -> JobPosting | None:
        with self._conn() as c:
            row = c.execute("select * from jobs where id = %s", (job_id,)).fetchone()
        return JobPosting.model_validate(row) if row else None

    def save_fit(self, job_id: str, fit: FitResult) -> None:
        with self._conn() as c:
            c.execute(
                """update jobs set fit = %s, fit_score = %s, injection_flags = %s,
                   scored_at = now() where id = %s""",
                (fit.model_dump_json(), fit.fit_score, json.dumps(fit.injection_flags), job_id),
            )

    def get_fit(self, job_id: str) -> FitResult | None:
        with self._conn() as c:
            row = c.execute("select fit from jobs where id = %s", (job_id,)).fetchone()
        return FitResult.model_validate(row["fit"]) if row and row["fit"] else None

    def set_stage(self, job_id: str, stage: Stage) -> None:
        with self._conn() as c:
            c.execute("update jobs set stage = %s where id = %s", (stage, job_id))

    def save_tailor(self, job_id: str, thread_id: str, status: str, **fields: Any) -> None:
        # CONSTRAINT: column names are interpolated into SQL, so only known columns pass.
        unknown = set(fields) - _TAILOR_COLS
        if unknown:
            raise ValueError(f"unknown tailor_runs columns: {unknown}")
        drafts = fields.get("drafts")
        if isinstance(drafts, Drafts):
            fields["drafts"] = drafts.model_dump_json()
        if "issues" in fields:
            fields["issues"] = json.dumps(fields["issues"])
        cols = ["job_id", "thread_id", "status", *fields]
        vals = [job_id, thread_id, status, *fields.values()]
        sets = ", ".join(f"{k} = excluded.{k}" for k in cols[1:])
        with self._conn() as c:
            c.execute(
                f"insert into tailor_runs ({', '.join(cols)}) values "
                f"({', '.join(['%s'] * len(cols))}) on conflict (job_id) do update set {sets}, "
                "updated_at = now()",
                vals,
            )
