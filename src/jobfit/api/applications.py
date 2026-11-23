"""Applications API (router mounted by api/app.py): list/filter, detail + timeline, manual and
quick-add logging, edits, the needs-review queue, and the background mail sync.

Depends on an AppStore (applications/store.py) and Deps (LLM for quick-add, graphs for sync).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from jobfit.api.tasks import TaskRunner
from jobfit.applications.match import company_score
from jobfit.applications.models import (
    AppEvent,
    Application,
    AppSource,
    AppStatus,
    Channel,
)
from jobfit.applications.quick_add import QuickAddDraft, parse_quick_add
from jobfit.applications.status import next_status
from jobfit.applications.store import AppStore
from jobfit.graphs.common import Deps
from jobfit.graphs.mail import FOLLOW_UP_KINDS, link_job

MAIL_KEY = "__mail__"  # TaskRunner key for the dashboard's Sync mail run


class ParseIn(BaseModel):
    text: str = Field(max_length=500)
    # WHY: the server runs in UTC (Cloud Run); "yesterday" must mean the user's yesterday.
    today: date | None = None


class CreateIn(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    applied_on: date | None = None
    channel: Channel = "other"
    resume_version: str | None = Field(default=None, max_length=200)
    salary_entered: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)
    source: Literal["manual", "quick_add"] = "manual"


class PatchIn(BaseModel):
    company: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    status: AppStatus | None = None
    channel: Channel | None = None
    resume_version: str | None = Field(default=None, max_length=200)
    salary_entered: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)
    needs_review: bool | None = None


class ReviewIn(BaseModel):
    action: Literal["assign", "create", "dismiss"]
    application_id: str | None = None
    role: str | None = Field(default=None, max_length=200)


def _at(d: date | None) -> datetime:
    return datetime.combine(d or date.today(), time(12), UTC)


def build_router(get: Callable[[], tuple[Deps, AppStore]], runner: TaskRunner) -> APIRouter:
    r = APIRouter(prefix="/api")

    def _app_or_404(store: AppStore, app_id: str) -> Application:
        app = store.get_app(app_id)
        if app is None:
            raise HTTPException(404, "application not found")
        return app

    @r.get("/applications")
    def list_apps(
        status: AppStatus | None = None,
        company: str | None = None,
        source: AppSource | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[dict[str, Any]]:
        _, store = get()
        out = []
        for a in store.list_apps():
            when = (a.applied_at or a.created_at).date()
            if status and a.status != status:
                continue
            if source and a.source != source:
                continue
            if company and not (
                company.lower() in a.company.lower() or company_score(company, a.company) >= 85
            ):
                continue
            if (date_from and when < date_from) or (date_to and when > date_to):
                continue
            out.append(a.model_dump(mode="json"))
        return out

    @r.get("/applications/summary")
    def summary() -> dict[str, Any]:
        return get()[1].summary()

    @r.get("/applications/review")
    def review_queue() -> dict[str, Any]:
        _, store = get()
        apps = [a for a in store.list_apps() if a.needs_review]
        return {
            "events": [e.model_dump(mode="json") for e in store.review_events()],
            "applications": [a.model_dump(mode="json") for a in apps],
        }

    @r.get("/applications/{app_id}")
    def detail(app_id: str) -> dict[str, Any]:
        deps, store = get()
        app = _app_or_404(store, app_id)
        job = deps.repo.get_job(app.job_id) if app.job_id else None
        return {
            **app.model_dump(mode="json"),
            "events": [e.model_dump(mode="json") for e in store.events_for(app_id)],
            "job": {"id": job.id, "company": job.company, "title": job.title} if job else None,
        }

    @r.post("/applications/parse")
    def parse(body: ParseIn) -> QuickAddDraft:
        """Preview only. Nothing is saved until the client posts the draft to /applications."""
        deps, _ = get()
        try:
            return parse_quick_add(body.text, deps, body.today)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:  # cost cap, quota, provider down
            raise HTTPException(503, f"couldn't parse right now: {type(e).__name__}") from e

    @r.post("/applications", status_code=201)
    def create(body: CreateIn) -> dict[str, Any]:
        _, store = get()
        at = _at(body.applied_on)
        app = Application(
            company=body.company.strip(),
            role=(body.role or "").strip() or None,
            source=body.source,
            channel=body.channel,
            applied_at=at,
            last_activity_at=at,
            resume_version=body.resume_version,
            salary_entered=body.salary_entered,
            notes=body.notes,
        )
        link_job(store, app)
        store.save_app(app)
        label = "Quick add" if body.source == "quick_add" else "Logged by hand"
        store.add_event(
            AppEvent(
                application_id=app.id,
                company=app.company,
                kind="manual_update",
                occurred_at=at,
                snippet=label,
            )
        )
        return app.model_dump(mode="json")

    @r.patch("/applications/{app_id}")
    def patch(app_id: str, body: PatchIn) -> dict[str, Any]:
        _, store = get()
        app = _app_or_404(store, app_id)
        changes = body.model_dump(exclude_unset=True)
        old_status = app.status
        app = app.model_copy(update=changes)
        if body.needs_review is False:
            app.review_reason = None
        if body.status and body.status != old_status:
            # Manual edits may set any status (e.g. withdrawn); the timeline records it.
            app.last_activity_at = datetime.now(UTC)
            store.add_event(
                AppEvent(
                    application_id=app.id,
                    company=app.company,
                    kind="status_change",
                    snippet=f"{old_status} -> {body.status} (by hand)",
                )
            )
        link_job(store, app)
        store.save_app(app)
        return app.model_dump(mode="json")

    @r.post("/applications/review/{event_id}")
    def resolve(event_id: str, body: ReviewIn) -> dict[str, Any]:
        _, store = get()
        ev = store.get_event(event_id)
        if ev is None:
            raise HTTPException(404, "review item not found")
        app: Application | None = None
        if body.action == "assign":
            if not body.application_id:
                raise HTTPException(400, "pick an application")
            app = _app_or_404(store, body.application_id)
        elif body.action == "create":
            if not ev.company:
                raise HTTPException(400, "this email has no company; dismiss it instead")
            app = Application(
                company=ev.company,
                role=(body.role or "").strip() or None,
                source="follow_up" if ev.kind in FOLLOW_UP_KINDS else "email",
                applied_at=None if ev.kind in FOLLOW_UP_KINDS else ev.occurred_at,
                last_activity_at=ev.occurred_at,
                status="applied",
            )
        if app is not None:
            app.status = next_status(app.status, ev.kind)
            app.last_activity_at = max(app.last_activity_at, ev.occurred_at)
            if body.role and not app.role:
                app.role = body.role.strip()
            link_job(store, app)
            store.save_app(app)
            ev.application_id = app.id
        ev.needs_review, ev.review_reason = False, None
        store.update_event(ev)
        return {
            "event": ev.model_dump(mode="json"),
            "application": app.model_dump(mode="json") if app else None,
        }

    # ---- mail sync (same as the daily run) --------------------------------------------------

    @r.post("/mail/sync", status_code=202)
    def start_sync() -> dict[str, str]:
        from jobfit.mail.sync import run_mail_sync
        from jobfit.mail.sync import summary as sync_summary

        def job() -> dict[str, Any]:
            deps, store = get()
            rep = run_mail_sync(deps, store)
            out = sync_summary(rep)
            if rep.status == "failed":
                out["error"] = "mail sync failed"
            return out

        if not runner.submit(MAIL_KEY, "mail", job):
            raise HTTPException(409, "a mail sync is already running")
        return {"status": "running"}

    @r.get("/mail/status")
    def mail_status() -> dict[str, Any]:
        from jobfit.mail.config import MailConfig
        from jobfit.mail.sync import build_fetcher

        _, store = get()
        cfg = MailConfig.load()
        state = store.sync_state()
        accounts = []
        for label, acct in cfg.accounts.items():
            st = state.get(label) or {}
            last = st.get("last_synced_at")
            accounts.append(
                {
                    "label": label,
                    "type": acct.type,
                    "configured": build_fetcher(label, acct, cfg) is not None,
                    "last_synced_at": last.isoformat() if isinstance(last, datetime) else last,
                    "last_error": st.get("last_error"),
                }
            )
        t = runner.get(MAIL_KEY)
        return {"accounts": accounts, "task": t.__dict__ if t else None}

    return r
