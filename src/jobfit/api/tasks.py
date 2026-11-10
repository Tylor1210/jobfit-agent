"""Background work for the API: scoring a newly added job (~30 s) and tailoring (2-4 min on the
Groq free tier). Requests return immediately; the dashboard polls job detail for progress.

Durable state lives in Postgres (jobs.fit, tailor_runs, the LangGraph checkpoint), so a paused
review survives a restart. Only "is a task running right now" is in memory.

CONSTRAINT: one task per job at a time; a second request for the same job is rejected (409).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("jobfit.api.tasks")


@dataclass
class TaskInfo:
    kind: str  # score | tailor | review
    started_at: str
    status: str = "running"  # running | done | failed
    error: str | None = None


class TaskRunner:
    def __init__(self, workers: int = 2) -> None:
        # WHY: 2 workers. More would only queue behind Groq's per-minute token limit anyway.
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jobfit")
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskInfo] = {}

    def get(self, job_id: str) -> TaskInfo | None:
        with self._lock:
            return self._tasks.get(job_id)

    def busy(self, job_id: str) -> bool:
        t = self.get(job_id)
        return t is not None and t.status == "running"

    def submit(self, job_id: str, kind: str, fn: Callable[[], Any]) -> bool:
        with self._lock:
            cur = self._tasks.get(job_id)
            if cur and cur.status == "running":
                return False
            info = TaskInfo(kind, datetime.now(UTC).isoformat(timespec="seconds"))
            self._tasks[job_id] = info

        def run() -> None:
            try:
                out = fn()
                err = out.get("error") if isinstance(out, dict) else None
                info.status, info.error = ("failed", str(err)) if err else ("done", None)
            except Exception as e:  # surfaced on the job page, full trace in the log
                log.exception("%s task for %s failed", kind, job_id)
                info.status, info.error = "failed", f"{type(e).__name__}: {e}"[:400]

        self._pool.submit(run)
        return True

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
