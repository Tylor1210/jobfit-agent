"""Read-side SQL for the dashboard (board, job detail, gaps, ops). Writes go through Repo.

Postgres only: the API needs DATABASE_URL. API tests run against local pgvector or the CI
Postgres service.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jobfit.config import ROOT
from jobfit.llm.client import ModelsConfig
from jobfit.store.db import SharedConnection

# Jobs shown on the board: anything scored, added by hand, or moved by Tylor. GOTCHA: auto-close
# sets stage 'closed' on never-scored jobs too; those are noise, so 'closed' alone doesn't count.
_BOARD_WHERE = "(fit is not null or source = 'manual' or stage not in ('new', 'closed'))"


class Dashboard:
    def __init__(self, dsn: str) -> None:
        self._db = SharedConnection(dsn, dict_rows=True)

    def _all(self, sql: str, params: Any = ()) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = self._db.run(lambda c: c.execute(sql, params).fetchall())
        return rows

    def board(self) -> list[dict[str, Any]]:
        return self._all(
            f"""select j.id, j.company, j.title, j.location, j.pay, j.remote, j.source, j.url,
                  j.stage, j.status, j.fit_score, j.fit->>'verdict' as verdict,
                  j.fit->'top_gaps'->>0 as top_gap, j.first_seen, j.scored_at,
                  t.status as tailor_status
                from jobs j left join tailor_runs t on t.job_id = j.id
                where {_BOARD_WHERE}
                order by j.fit_score desc nulls last, j.first_seen desc"""
        )

    def job(self, job_id: str) -> dict[str, Any] | None:
        rows = self._all(
            """select j.*, t.thread_id, t.status as tailor_status, t.drafts, t.issues,
                 exists(select 1 from tailor_pdfs p where p.job_id = j.id and p.kind = 'resume')
                   as has_resume_pdf,
                 exists(select 1 from tailor_pdfs p where p.job_id = j.id and p.kind = 'cover')
                   as has_cover_pdf,
                 t.trace_url as tailor_trace_url,
                 t.updated_at as tailor_updated_at
               from jobs j left join tailor_runs t on t.job_id = j.id where j.id = %s""",
            (job_id,),
        )
        return rows[0] if rows else None

    def gap_rows(self) -> list[dict[str, Any]]:
        return self._all(
            "select id, company, title, fit_score, fit from jobs where fit is not null"
        )

    def ops(self) -> dict[str, Any]:
        cfg = ModelsConfig.load()
        runs = self._all(
            """select id, started_at, finished_at, status, stats, prefiltered, scored,
                 spend_usd, notes from ingest_runs order by started_at desc limit 10"""
        )
        spend = self._all(
            """select (created_at at time zone 'utc')::date as day, sum(cost_usd) as usd,
                 sum(input_tokens) as input_tokens, sum(output_tokens) as output_tokens,
                 count(*) as calls
               from llm_usage where created_at > now() - interval '14 days'
               group by 1 order by 1"""
        )
        evals = self._all("select run_at, summary from eval_runs order by run_at desc limit 20")
        if not evals:
            evals = _history_file()
        traces = self._all(
            """select id, company, title, scored_at as at, 'fit' as kind,
                 fit->>'trace_url' as url
               from jobs where fit->>'trace_url' is not null
               union all
               select j.id, j.company, j.title, t.updated_at, 'tailor', t.trace_url
               from tailor_runs t join jobs j on j.id = t.job_id where t.trace_url is not null
               order by at desc limit 12"""
        )
        counts = self._all(
            """select count(*) filter (where status = 'open') as open,
                 count(*) filter (where status = 'closed') as closed,
                 count(*) filter (where fit is not null) as scored,
                 count(*) filter (where prefilter = 'pass' and fit is null
                                  and status = 'open') as queued
               from jobs"""
        )[0]
        today = spend[-1]["usd"] if spend and str(spend[-1]["day"]) == _today() else 0
        return {
            "runs": runs,
            "spend": spend,
            "evals": evals,
            "traces": traces,
            "counts": counts,
            "today_usd": float(today or 0),
            "cap_usd": cfg.daily_cost_cap_usd,
            "provider": cfg.provider,
        }


def _today() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).date().isoformat()


def _history_file(path: Path = ROOT / "evals" / "history.jsonl") -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [{"run_at": r["run_at"], "summary": r} for r in reversed(rows[-20:])]
