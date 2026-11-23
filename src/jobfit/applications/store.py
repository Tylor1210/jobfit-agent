"""Persistence for applications, their events, and mail sync state (in-memory + Postgres).

Called by the mail graph, mail sync, the quick-add/manual API, and the evals. The in-memory store
backs unit tests and evals; PgAppStore needs migration 006.
"""

from __future__ import annotations

import json
from collections import Counter
from contextlib import AbstractContextManager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from jobfit.applications.models import AppEvent, Application, now
from jobfit.applications.status import is_ghosted

if TYPE_CHECKING:
    import psycopg

    from jobfit.store.repo import InMemoryRepo

# Job board stages a linked application may move a card to, in forward order.
_STAGE_ORDER = ["new", "shortlisted", "applied", "interview", "offer"]
APP_TO_STAGE = {
    "applied": "applied",
    "screening": "applied",
    "interview": "interview",
    "offer": "offer",
    "rejected": "rejected",
}


def stage_after(current: str, target: str) -> str | None:
    """The job card stage to set, or None to leave it. Only moves forward; never touches a card
    Tylor parked in rejected/closed by hand unless email says rejected."""
    if current == target:
        return None
    if target == "rejected":
        return None if current in ("offer",) else "rejected"
    if current in ("rejected", "closed"):
        return None
    if current in _STAGE_ORDER and target in _STAGE_ORDER:
        return target if _STAGE_ORDER.index(target) > _STAGE_ORDER.index(current) else None
    return None


def summarize(apps: list[Application], review_events: int) -> dict[str, Any]:
    matched = sum(1 for a in apps if a.job_id)
    return {
        "total": len(apps),
        "matched_postings": matched,
        "new": len(apps) - matched,
        "needs_review": sum(1 for a in apps if a.needs_review) + review_events,
        "by_status": dict(Counter(a.status for a in apps)),
        "by_source": dict(Counter(a.source for a in apps)),
    }


class AppStore(Protocol):
    def list_apps(self) -> list[Application]: ...
    def get_app(self, app_id: str) -> Application | None: ...
    def save_app(self, app: Application) -> None: ...
    def has_message(self, message_id: str) -> bool: ...
    def add_event(self, ev: AppEvent) -> bool: ...
    def get_event(self, event_id: str) -> AppEvent | None: ...
    def update_event(self, ev: AppEvent) -> None: ...
    def events_for(self, app_id: str) -> list[AppEvent]: ...
    def review_events(self) -> list[AppEvent]: ...
    def job_candidates(self) -> list[dict[str, Any]]: ...
    def set_job_stage(self, job_id: str, stage: str) -> None: ...
    def sync_state(self) -> dict[str, dict[str, Any]]: ...
    def set_sync_state(
        self, account: str, *, synced_at: datetime | None, error: str | None
    ) -> None: ...
    def mark_ghosted(self, at: datetime) -> int: ...
    def summary(self) -> dict[str, Any]: ...
    def start_run(self) -> int: ...
    def finish_run(self, run_id: int, **fields: Any) -> None: ...


class InMemoryAppStore:
    def __init__(self, repo: InMemoryRepo | None = None) -> None:
        self.repo = repo
        self.apps: dict[str, Application] = {}
        self.events: dict[str, AppEvent] = {}
        self.state: dict[str, dict[str, Any]] = {}
        self.runs: dict[int, dict[str, Any]] = {}

    def list_apps(self) -> list[Application]:
        return sorted(self.apps.values(), key=lambda a: a.last_activity_at, reverse=True)

    def get_app(self, app_id: str) -> Application | None:
        return self.apps.get(app_id)

    def save_app(self, app: Application) -> None:
        self.apps[app.id] = app

    def has_message(self, message_id: str) -> bool:
        return any(e.message_id == message_id for e in self.events.values())

    def add_event(self, ev: AppEvent) -> bool:
        if ev.message_id and self.has_message(ev.message_id):
            return False
        self.events[ev.id] = ev
        return True

    def get_event(self, event_id: str) -> AppEvent | None:
        return self.events.get(event_id)

    def update_event(self, ev: AppEvent) -> None:
        self.events[ev.id] = ev

    def events_for(self, app_id: str) -> list[AppEvent]:
        evs = [e for e in self.events.values() if e.application_id == app_id]
        return sorted(evs, key=lambda e: e.occurred_at)

    def review_events(self) -> list[AppEvent]:
        return [e for e in self.events.values() if e.needs_review]

    def job_candidates(self) -> list[dict[str, Any]]:
        if not self.repo:
            return []
        return [
            {"id": j.id, "company": j.company, "title": j.title, "stage": j.stage}
            for j in self.repo.jobs.values()
        ]

    def set_job_stage(self, job_id: str, stage: str) -> None:
        if self.repo:
            self.repo.set_stage(job_id, stage)  # type: ignore[arg-type]

    def sync_state(self) -> dict[str, dict[str, Any]]:
        return self.state

    def set_sync_state(
        self, account: str, *, synced_at: datetime | None, error: str | None
    ) -> None:
        cur = self.state.setdefault(account, {"last_synced_at": None, "last_error": None})
        if synced_at:
            cur["last_synced_at"] = synced_at
        cur["last_error"] = error

    def mark_ghosted(self, at: datetime) -> int:
        n = 0
        for a in list(self.apps.values()):
            if is_ghosted(a.status, a.last_activity_at, at):
                self.apps[a.id] = a.model_copy(update={"status": "ghosted"})
                n += 1
        return n

    def summary(self) -> dict[str, Any]:
        return summarize(self.list_apps(), len(self.review_events()))

    def start_run(self) -> int:
        rid = len(self.runs) + 1
        self.runs[rid] = {"status": "running"}
        return rid

    def finish_run(self, run_id: int, **fields: Any) -> None:
        self.runs[run_id].update(fields)


_APP_COLS = [
    "id",
    "company",
    "company_norm",
    "role",
    "status",
    "source",
    "channel",
    "applied_at",
    "last_activity_at",
    "job_id",
    "resume_version",
    "salary_entered",
    "notes",
    "needs_review",
    "review_reason",
    "created_at",
]
_EVENT_COLS = [
    "id",
    "application_id",
    "company",
    "kind",
    "occurred_at",
    "account",
    "message_id",
    "snippet",
    "extracted",
    "confidence",
    "injection_flags",
    "needs_review",
    "review_reason",
]


def _app_row(app: Application) -> dict[str, Any]:
    from jobfit.applications.match import norm_company

    return {**app.model_dump(), "company_norm": norm_company(app.company)}


def _event_row(ev: AppEvent) -> dict[str, Any]:
    d = ev.model_dump()
    d["extracted"] = json.dumps(d["extracted"])
    d["injection_flags"] = json.dumps(d["injection_flags"])
    return d


class PgAppStore:
    def __init__(self, dsn: str) -> None:
        from jobfit.store.db import SharedConnection

        self._db = SharedConnection(dsn, dict_rows=True)

    def _conn(self) -> AbstractContextManager[psycopg.Connection[Any]]:
        return self._db.use()

    def _all(self, sql: str, params: Any = ()) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = self._db.run(lambda c: c.execute(sql, params).fetchall())
        return rows

    @staticmethod
    def _app(r: dict[str, Any]) -> Application:
        return Application.model_validate({k: v for k, v in r.items() if k != "company_norm"})

    def list_apps(self) -> list[Application]:
        return [
            self._app(r)
            for r in self._all("select * from applications order by last_activity_at desc")
        ]

    def get_app(self, app_id: str) -> Application | None:
        rows = self._all("select * from applications where id = %s", (app_id,))
        return self._app(rows[0]) if rows else None

    def save_app(self, app: Application) -> None:
        cols = ", ".join(_APP_COLS)
        vals = ", ".join(f"%({c})s" for c in _APP_COLS)
        upd = ", ".join(f"{c} = excluded.{c}" for c in _APP_COLS if c not in ("id", "created_at"))
        with self._conn() as c:
            c.execute(
                f"""insert into applications ({cols}) values ({vals})
                    on conflict (id) do update set {upd}, updated_at = now()""",
                _app_row(app),
            )

    def has_message(self, message_id: str) -> bool:
        return bool(
            self._all("select 1 from application_events where message_id = %s", (message_id,))
        )

    def add_event(self, ev: AppEvent) -> bool:
        cols = ", ".join(_EVENT_COLS)
        vals = ", ".join(f"%({c})s" for c in _EVENT_COLS)
        with self._conn() as c:
            cur = c.execute(
                f"""insert into application_events ({cols}) values ({vals})
                    on conflict (message_id) where message_id is not null do nothing""",
                _event_row(ev),
            )
            return cur.rowcount == 1

    def get_event(self, event_id: str) -> AppEvent | None:
        rows = self._all("select * from application_events where id = %s", (event_id,))
        return AppEvent.model_validate(rows[0]) if rows else None

    def update_event(self, ev: AppEvent) -> None:
        sets = ", ".join(f"{c} = %({c})s" for c in _EVENT_COLS if c != "id")
        with self._conn() as c:
            c.execute(f"update application_events set {sets} where id = %(id)s", _event_row(ev))

    def events_for(self, app_id: str) -> list[AppEvent]:
        return [
            AppEvent.model_validate(r)
            for r in self._all(
                "select * from application_events where application_id = %s order by occurred_at",
                (app_id,),
            )
        ]

    def review_events(self) -> list[AppEvent]:
        return [
            AppEvent.model_validate(r)
            for r in self._all(
                "select * from application_events where needs_review order by occurred_at desc"
            )
        ]

    def job_candidates(self) -> list[dict[str, Any]]:
        return self._all("select id, company, title, stage from jobs")

    def set_job_stage(self, job_id: str, stage: str) -> None:
        with self._conn() as c:
            c.execute("update jobs set stage = %s where id = %s", (stage, job_id))

    def sync_state(self) -> dict[str, dict[str, Any]]:
        return {r["account"]: r for r in self._all("select * from mail_sync_state")}

    def set_sync_state(
        self, account: str, *, synced_at: datetime | None, error: str | None
    ) -> None:
        with self._conn() as c:
            c.execute(
                """insert into mail_sync_state (account, last_synced_at, last_error)
                   values (%s, %s, %s)
                   on conflict (account) do update set
                     last_synced_at = coalesce(excluded.last_synced_at,
                                               mail_sync_state.last_synced_at),
                     last_error = excluded.last_error, updated_at = now()""",
                (account, synced_at, error),
            )

    def mark_ghosted(self, at: datetime) -> int:
        from jobfit.applications.status import GHOST_DAYS

        with self._conn() as c:
            cur = c.execute(
                """update applications set status = 'ghosted', updated_at = now()
                   where status in ('applied', 'screening', 'interview')
                     and last_activity_at <= %s - make_interval(days => %s)""",
                (at, GHOST_DAYS),
            )
            return cur.rowcount

    def summary(self) -> dict[str, Any]:
        n = self._all("select count(*) as n from application_events where needs_review")
        return summarize(self.list_apps(), int(n[0]["n"]))

    def start_run(self) -> int:
        rows = self._all("insert into ingest_runs (kind) values ('mail') returning id")
        return int(rows[0]["id"])

    def finish_run(self, run_id: int, **fields: Any) -> None:
        allowed = {"status", "stats", "notes", "spend_usd"}
        if set(fields) - allowed:  # CONSTRAINT: column names are interpolated
            raise ValueError(f"unknown ingest_runs columns: {set(fields) - allowed}")
        data = {k: json.dumps(v) if k in ("stats", "notes") else v for k, v in fields.items()}
        sets = ", ".join(f"{k} = %({k})s" for k in data)
        with self._conn() as c:
            c.execute(
                f"update ingest_runs set {sets}, finished_at = now() where id = %(rid)s",
                {**data, "rid": run_id},
            )


__all__ = ["APP_TO_STAGE", "AppStore", "InMemoryAppStore", "PgAppStore", "now", "stage_after"]
