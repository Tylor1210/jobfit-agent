"""FastAPI app: JSON API for the dashboard under /api, and the built React app (web/dist) at /.

Run: `jobfit serve` (or `uvicorn jobfit.api.app:create_app --factory`).
Depends on DATABASE_URL (Postgres/Supabase). LLM, tracing and renderer come from jobfit.app.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from jobfit.api.gaps import aggregate
from jobfit.api.queries import Dashboard
from jobfit.api.tasks import TaskRunner
from jobfit.config import ROOT, get_settings
from jobfit.schemas import CoverLetterOut, ResumeOut, Stage

WEB_DIST = ROOT / "web" / "dist"


class StageIn(BaseModel):
    stage: Stage


class AddJobIn(BaseModel):
    url: str | None = None
    text: str | None = None
    company: str | None = None
    title: str | None = None
    location: str | None = None


class ReviewIn(BaseModel):
    action: Literal["approve", "edit", "revise"]
    resume: ResumeOut | None = None
    cover_letter: CoverLetterOut | None = None
    note: str | None = Field(default=None, max_length=2000)


def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def create_app(
    *,
    deps: Any = None,
    dashboard: Dashboard | None = None,
    checkpointer: Any = None,
    serve_web: bool = True,
) -> FastAPI:
    """Factory. Tests pass their own deps/dashboard/checkpointer; production builds them lazily
    on startup so importing the module stays cheap."""
    state: dict[str, Any] = {"deps": deps, "dash": dashboard, "saver": checkpointer}
    runner = TaskRunner()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        closer = None
        if state["dash"] is None:
            dsn = get_settings().database_url
            if not dsn:
                raise RuntimeError("DATABASE_URL is required to serve the dashboard")
            state["dash"] = Dashboard(dsn)
        if state["deps"] is None:
            from jobfit.app import build_deps

            state["deps"] = build_deps(render=True)
        if state["saver"] is None:
            from jobfit.app import build_checkpointer

            state["saver"], closer = build_checkpointer()
        yield
        runner.shutdown()
        if closer:
            closer.__exit__(None, None, None)

    app = FastAPI(title="Job Fit Agent", lifespan=lifespan)

    def dash() -> Dashboard:
        d: Dashboard = state["dash"]
        return d

    # ---- board -------------------------------------------------------------------------------

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        rows = dash().board()
        for r in rows:
            t = runner.get(r["id"])
            r["task"] = t.__dict__ if t else None
        return rows

    @app.patch("/api/jobs/{job_id}/stage")
    def set_stage(job_id: str, body: StageIn) -> dict[str, str]:
        if dash().job(job_id) is None:
            raise HTTPException(404, "job not found")
        state["deps"].repo.set_stage(job_id, body.stage)
        return {"id": job_id, "stage": body.stage}

    # ---- detail ------------------------------------------------------------------------------

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str) -> dict[str, Any]:
        row = dash().job(job_id)
        if row is None:
            raise HTTPException(404, "job not found")
        for k in ("fit", "drafts", "issues", "injection_flags"):
            row[k] = _json(row.get(k))
        t = runner.get(job_id)
        row["task"] = t.__dict__ if t else None
        row["has_resume_pdf"] = bool(row.get("resume_pdf") and Path(row["resume_pdf"]).exists())
        row["has_cover_pdf"] = bool(row.get("cover_pdf") and Path(row["cover_pdf"]).exists())
        for k in ("resume_pdf", "cover_pdf"):  # local paths are not the client's business
            row.pop(k, None)
        return row

    @app.get("/api/evidence")
    def evidence() -> dict[str, str]:
        """Every citable evidence ID with its text, for clickable citations in the UI."""
        idx: dict[str, str] = state["deps"].profile.evidence_index()
        return idx

    # ---- add job -----------------------------------------------------------------------------

    @app.post("/api/jobs", status_code=202)
    def add_job(body: AddJobIn) -> dict[str, str]:
        from jobfit.graphs.fit import run_fit
        from jobfit.ingest.manual import ManualAddError, from_text, from_url

        try:
            if body.text:
                if not (body.company and body.title):
                    raise ManualAddError("company and title are required with pasted text")
                job = from_text(
                    body.text, body.company, body.title, url=body.url, location=body.location
                )
            elif body.url:
                job = from_url(body.url)
                upd = {k: v for k in ("company", "title", "location") if (v := getattr(body, k))}
                job = job.model_copy(update=upd)
            else:
                raise ManualAddError("paste a URL or the posting text")
        except ManualAddError as e:
            raise HTTPException(400, str(e)) from e
        deps = state["deps"]
        deps.repo.upsert_job(job)
        runner.submit(job.id, "score", lambda: run_fit(job, deps))
        return {"id": job.id}

    @app.post("/api/jobs/{job_id}/score", status_code=202)
    def rescore(job_id: str) -> dict[str, str]:
        from jobfit.graphs.fit import run_fit

        job = state["deps"].repo.get_job(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if not runner.submit(job_id, "score", lambda: run_fit(job, state["deps"])):
            raise HTTPException(409, "this job already has a task running")
        return {"id": job_id}

    # ---- tailoring (the human approval step) -------------------------------------------------

    @app.post("/api/jobs/{job_id}/tailor", status_code=202)
    def tailor(job_id: str) -> dict[str, str]:
        from jobfit.graphs.tailor import new_thread_id, start_tailor

        deps = state["deps"]
        if deps.repo.get_fit(job_id) is None:
            raise HTTPException(409, "score this job before tailoring")
        thread_id = new_thread_id(job_id)
        if runner.busy(job_id):
            raise HTTPException(409, "this job already has a task running")
        deps.repo.save_tailor(job_id, thread_id, "drafting", issues=[])
        runner.submit(
            job_id,
            "tailor",
            lambda: start_tailor(job_id, deps, state["saver"], thread_id=thread_id),
        )
        return {"id": job_id, "thread_id": thread_id}

    @app.post("/api/jobs/{job_id}/review", status_code=202)
    def review(job_id: str, body: ReviewIn) -> dict[str, str]:
        from jobfit.graphs.tailor import resume_tailor

        row = dash().job(job_id)
        if row is None or not row.get("thread_id"):
            raise HTTPException(404, "no tailoring run for this job")
        if row.get("tailor_status") != "awaiting_review":
            raise HTTPException(
                409, f"tailoring is {row.get('tailor_status')}, not awaiting review"
            )
        decision: dict[str, Any] = {"action": body.action}
        if body.action == "edit":
            if body.resume is None and body.cover_letter is None:
                raise HTTPException(400, "edit needs a resume or cover_letter")
            if body.resume:
                decision["resume"] = body.resume.model_dump()
            if body.cover_letter:
                decision["cover_letter"] = body.cover_letter.model_dump()
        if body.action == "revise":
            if not (body.note and body.note.strip()):
                raise HTTPException(400, "send-back needs a note")
            decision["note"] = body.note
        deps = state["deps"]
        nxt = {"approve": "rendering", "edit": "checking", "revise": "drafting"}[body.action]
        deps.repo.save_tailor(job_id, row["thread_id"], nxt)
        ok = runner.submit(
            job_id,
            "review",
            lambda: resume_tailor(job_id, row["thread_id"], decision, deps, state["saver"]),
        )
        if not ok:
            raise HTTPException(409, "this job already has a task running")
        return {"id": job_id, "status": nxt}

    @app.get("/api/jobs/{job_id}/pdf/{kind}")
    def pdf(job_id: str, kind: Literal["resume", "cover"]) -> FileResponse:
        row = dash().job(job_id)
        path = row and row.get(f"{kind}_pdf")
        if not path or not Path(path).exists():
            raise HTTPException(404, "PDF not rendered yet")
        return FileResponse(path, media_type="application/pdf", filename=Path(path).name)

    # ---- insights ----------------------------------------------------------------------------

    @app.get("/api/gaps")
    def gaps(min_fit: int = 0, soft: bool = False) -> list[dict[str, Any]]:
        return aggregate(dash().gap_rows(), min_fit=min_fit, include_soft=soft)

    @app.get("/api/ops")
    def ops() -> dict[str, Any]:
        return dash().ops()

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ---- web app -----------------------------------------------------------------------------

    if serve_web and WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            # SPA fallback: client-side routes (/jobs/x, /gaps) all load index.html.
            f = WEB_DIST / path
            if path and f.is_file() and WEB_DIST in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(WEB_DIST / "index.html")

    return app
