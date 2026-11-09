"""Daily cost ledger and cap. Checked before every LLM call, written after.

Postgres ledger in production (table llm_usage, db/migrations/002_jobs.sql), in-memory for tests.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol


class CostCapExceeded(RuntimeError):
    """Today's spend reached the cap. Graph runs stop cleanly and the dashboard shows it."""

    def __init__(self, spent: float, cap: float) -> None:
        super().__init__(f"daily cost cap reached: ${spent:.4f} of ${cap:.2f}")
        self.spent = spent
        self.cap = cap


@dataclass(frozen=True, slots=True)
class UsageRecord:
    run_id: str
    node: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int


class CostLedger(Protocol):
    def spent_today(self) -> float: ...
    def record(self, rec: UsageRecord) -> None: ...


def _today() -> date:
    # WHY: one clock for the cap (UTC) so CI, cron and local runs agree on when a day ends.
    return datetime.now(UTC).date()


class InMemoryLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.records: list[tuple[date, UsageRecord]] = []

    def spent_today(self) -> float:
        d = _today()
        with self._lock:
            return sum(r.cost_usd for day, r in self.records if day == d)

    def record(self, rec: UsageRecord) -> None:
        with self._lock:
            self.records.append((_today(), rec))


class PgLedger:
    def __init__(self, dsn: str) -> None:
        from jobfit.store.db import SharedConnection

        self._db = SharedConnection(dsn)

    def spent_today(self) -> float:
        with self._db.use() as conn:
            row = conn.execute(
                "select coalesce(sum(cost_usd), 0) from llm_usage "
                "where created_at >= date_trunc('day', now() at time zone 'utc') at time zone 'utc'"
            ).fetchone()
        return float(row[0]) if row else 0.0

    def record(self, rec: UsageRecord) -> None:
        with self._db.use() as conn:
            conn.execute(
                """insert into llm_usage (run_id, node, provider, model, input_tokens,
                   output_tokens, cost_usd, latency_ms) values (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    rec.run_id,
                    rec.node,
                    rec.provider,
                    rec.model,
                    rec.input_tokens,
                    rec.output_tokens,
                    rec.cost_usd,
                    rec.latency_ms,
                ),
            )
