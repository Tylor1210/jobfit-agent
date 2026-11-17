"""PgAppStore against real Postgres (local pgvector or the CI service). Needs DATABASE_URL."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from jobfit.applications.models import AppEvent, Application

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs DATABASE_URL"),
]
CO = "PytestCo Mail"


@pytest.fixture
def store() -> Iterator[object]:
    import psycopg

    from jobfit.applications.store import PgAppStore
    from jobfit.cli import _migrate

    _migrate()
    dsn = os.environ["DATABASE_URL"]
    yield PgAppStore(dsn)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("delete from application_events where company = %s", (CO,))
        c.execute("delete from applications where company = %s", (CO,))
        c.execute("delete from mail_sync_state where account = 'pytest_acct'")
        c.execute("delete from ingest_runs where kind = 'mail' and stats->>'pytest' = '1'")


def test_round_trip_dedup_ghosting(store: object) -> None:
    from jobfit.applications.store import PgAppStore

    assert isinstance(store, PgAppStore)
    old = datetime.now(UTC) - timedelta(days=40)
    a = Application(company=CO, role="AI Engineer", source="email", last_activity_at=old)
    store.save_app(a)
    a.notes = "updated"
    store.save_app(a)  # upsert
    got = store.get_app(a.id)
    assert got and got.notes == "updated" and got.role == "AI Engineer"

    mid = f"<{a.id}@pytest>"
    ev = AppEvent(
        application_id=a.id,
        company=CO,
        kind="application_received",
        message_id=mid,
        snippet="x" * 200,
        extracted={"role": "AI Engineer"},
        injection_flags=["f"],
    )
    assert store.add_event(ev)
    assert not store.add_event(AppEvent(company=CO, kind="rejection", message_id=mid))
    assert store.has_message(mid)
    (back,) = store.events_for(a.id)
    assert back.extracted["role"] == "AI Engineer" and back.injection_flags == ["f"]

    back.needs_review = True
    store.update_event(back)
    assert any(e.id == back.id for e in store.review_events())

    assert store.mark_ghosted(datetime.now(UTC)) >= 1
    assert (store.get_app(a.id) or a).status == "ghosted"

    store.set_sync_state("pytest_acct", synced_at=datetime.now(UTC), error=None)
    store.set_sync_state("pytest_acct", synced_at=None, error="boom")
    st = store.sync_state()["pytest_acct"]
    assert st["last_synced_at"] is not None and st["last_error"] == "boom"  # cursor kept

    rid = store.start_run()
    store.finish_run(rid, status="ok", stats={"pytest": "1"}, spend_usd=0)
    assert store.summary()["total"] >= 1


def test_snippet_limit_is_enforced_by_the_db(store: object) -> None:
    import psycopg

    from jobfit.applications.store import PgAppStore

    assert isinstance(store, PgAppStore)
    with pytest.raises(psycopg.errors.CheckViolation):
        store.add_event(AppEvent(company=CO, kind="other", snippet="x" * 201))
