"""One reusable Postgres connection per store, with timeouts. Used by every Pg* class.

WHY: opening a TLS connection through the Supabase pooler costs ~0.3-1s, and the fit graph used
to open ~90 per job (one per vector search). A dead socket with no timeouts also hung a daily run
for 2 hours. Now each store keeps one connection, every socket has connect/keepalive limits,
every statement has a server-side timeout, and a broken connection is replaced on next use.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

CONNECT_TIMEOUT_S = 10
STATEMENT_TIMEOUT_MS = 20_000


def connect(dsn: str, *, dict_rows: bool = False) -> psycopg.Connection[Any]:
    conn: psycopg.Connection[Any] = psycopg.connect(
        dsn,
        autocommit=True,
        connect_timeout=CONNECT_TIMEOUT_S,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
        row_factory=dict_row if dict_rows else psycopg.rows.tuple_row,
        # GOTCHA: prepared statements break behind PgBouncer/Supavisor in transaction mode.
        prepare_threshold=None,
    )
    # SET rather than a startup `options=`: the Supabase pooler rejects startup options.
    conn.execute(f"set statement_timeout = {STATEMENT_TIMEOUT_MS}")
    return conn


class SharedConnection:
    def __init__(
        self,
        dsn: str,
        *,
        dict_rows: bool = False,
        on_connect: Callable[[psycopg.Connection[Any]], None] | None = None,
    ) -> None:
        self._dsn, self._dict_rows, self._on_connect = dsn, dict_rows, on_connect
        self._conn: psycopg.Connection[Any] | None = None
        # GOTCHA: a psycopg connection must not be used by two threads at once. LangGraph runs
        # nodes sequentially, but the lock keeps the API (Phase 5) safe too.
        self._lock = threading.RLock()

    def _live(self) -> psycopg.Connection[Any]:
        if self._conn is None or self._conn.closed or self._conn.broken:
            self._conn = connect(self._dsn, dict_rows=self._dict_rows)
            if self._on_connect:
                self._on_connect(self._conn)
        return self._conn

    @contextmanager
    def use(self) -> Iterator[psycopg.Connection[Any]]:
        """Yield the shared connection. On a connection-level failure, drop it so the next
        call reconnects. Does not close the connection on normal exit."""
        with self._lock:
            conn = self._live()
            try:
                yield conn
            except psycopg.OperationalError:
                self.close()
                raise

    def run[T](self, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
        """Run an idempotent operation, retrying once on a dropped connection."""
        try:
            with self.use() as c:
                return fn(c)
        except psycopg.OperationalError:
            with self.use() as c:
                return fn(c)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
