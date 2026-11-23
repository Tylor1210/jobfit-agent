"""Gmail/IMAP connectors and mail sync, offline (httpx MockTransport, fake IMAP, fake fetchers)."""

from __future__ import annotations

import base64
import imaplib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from jobfit.applications.models import MailClassOut, MailMessage
from jobfit.applications.store import InMemoryAppStore
from jobfit.graphs.common import Deps
from jobfit.llm.cost import UsageRecord
from jobfit.llm.fake import FakeProvider
from jobfit.mail.auth import write_env
from jobfit.mail.config import MailAuthExpired, MailConfig
from jobfit.mail.gmail import GmailFetcher, build_query
from jobfit.mail.imap import ImapFetcher, build_search
from jobfit.mail.sync import run_mail_sync

CFG = MailConfig.load()
SINCE = datetime(2026, 8, 3, tzinfo=UTC)


def test_gmail_query_filters_server_side() -> None:
    q = build_query(CFG, SINCE)
    assert q.startswith(f"after:{int(SINCE.timestamp())} ")
    assert "from:(greenhouse.io OR lever.co" in q and "linkedin.com)" in q
    assert "subject:(application OR interview OR assessment OR offer)" in q
    assert "-from:jobalerts-noreply@linkedin.com" in q and "-category:promotions" in q


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def test_gmail_fetch_parses_messages() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "at"})
        if req.url.path.endswith("/messages"):
            assert "after:" in req.url.params["q"]
            return httpx.Response(200, json={"messages": [{"id": "g1"}]})
        assert req.headers["Authorization"] == "Bearer at"
        payload = {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "Acme <no-reply@greenhouse.io>"},
                {"name": "Subject", "value": "Thanks for applying"},
                {"name": "Message-ID", "value": "<abc@mail.test>"},
            ],
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64("<p>html</p>")}},
                {"mimeType": "text/plain", "body": {"data": _b64("plain body")}},
            ],
        }
        return httpx.Response(
            200, json={"id": "g1", "internalDate": "1790000000000", "payload": payload}
        )

    f = GmailFetcher(
        "gmail_main",
        "cid",
        "cs",
        "rt",
        "GMAIL_MAIN_REFRESH_TOKEN",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    (m,) = list(f.fetch(CFG, SINCE))
    assert (m.message_id, m.subject, m.body, m.account) == (
        "<abc@mail.test>",
        "Thanks for applying",
        "plain body",
        "gmail_main",
    )


def test_gmail_expired_token_has_a_clear_message() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    f = GmailFetcher(
        "gmail_alt",
        "cid",
        "cs",
        "rt",
        "GMAIL_ALT_REFRESH_TOKEN",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(MailAuthExpired) as e:
        list(f.fetch(CFG, SINCE))
    msg = str(e.value)
    assert "jobfit mail auth gmail_alt" in msg and "GMAIL_ALT_REFRESH_TOKEN" in msg


def test_imap_search_string() -> None:
    s = build_search(CFG, SINCE)
    assert s.startswith('SINCE 03-Aug-2026 (OR FROM "greenhouse.io" OR FROM "lever.co"')
    assert 'SUBJECT "offer")' in s and 'NOT FROM "jobalerts-noreply@linkedin.com"' in s


RAW = (
    b"From: Talent <jobs@lever.co>\r\nSubject: Your application\r\n"
    b"Message-ID: <y1@mail.test>\r\nDate: Mon, 05 Oct 2026 10:00:00 +0000\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\nWe received your application.\r\n"
)


class FakeImap:
    def __init__(self, fail_login: bool = False) -> None:
        self.fail_login = fail_login
        self.calls: list[tuple[Any, ...]] = []

    def login(self, u: str, p: str) -> tuple[str, list[bytes]]:
        if self.fail_login:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        return "OK", [b""]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", folder, readonly))
        return "OK", [b"1"]

    def uid(self, cmd: str, *args: str) -> tuple[str, list[Any]]:
        self.calls.append(("uid", cmd, *args))
        if cmd == "SEARCH":
            return "OK", [b"7"]
        return "OK", [(b"7 (BODY[] {120}", RAW), b")"]

    def logout(self) -> None:
        self.calls.append(("logout",))


def _conn(fake: FakeImap) -> Any:
    return lambda host: fake


def test_imap_is_read_only() -> None:
    fake = FakeImap()
    f = ImapFetcher(
        "yahoo", "imap.test", "u", "p", ["INBOX"], "YAHOO_APP_PASSWORD", connect=_conn(fake)
    )
    (m,) = list(f.fetch(CFG, SINCE))
    assert ("select", "INBOX", True) in fake.calls  # EXAMINE, not SELECT
    fetches = [c for c in fake.calls if c[:2] == ("uid", "FETCH")]
    assert fetches and all("BODY.PEEK[]" in c[3] for c in fetches)
    assert not any(c[:2] == ("uid", "STORE") or c[:2] == ("uid", "COPY") for c in fake.calls)
    assert (m.message_id, m.subject) == ("<y1@mail.test>", "Your application")
    assert "We received your application." in m.body


def test_imap_bad_password_is_auth_expired() -> None:
    f = ImapFetcher(
        "yahoo",
        "imap.test",
        "u",
        "p",
        ["INBOX"],
        "YAHOO_APP_PASSWORD",
        connect=_conn(FakeImap(fail_login=True)),
    )
    with pytest.raises(MailAuthExpired, match="YAHOO_APP_PASSWORD"):
        list(f.fetch(CFG, SINCE))


def test_write_env_replaces_or_appends(tmp_path: Any) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\nGMAIL_MAIN_REFRESH_TOKEN=old\n")
    write_env("GMAIL_MAIN_REFRESH_TOKEN", "new", str(env))
    write_env("B", "2", str(env))
    assert env.read_text() == "A=1\nGMAIL_MAIN_REFRESH_TOKEN=new\nB=2\n"


# ---- sync -------------------------------------------------------------------------------------

AT = datetime(2026, 11, 1, tzinfo=UTC)


class ListFetcher:
    def __init__(self, msgs: list[MailMessage] | Exception) -> None:
        self.msgs = msgs
        self.since: list[datetime] = []

    def fetch(self, cfg: MailConfig, since: datetime) -> list[MailMessage]:
        self.since.append(since)
        if isinstance(self.msgs, Exception):
            raise self.msgs
        return self.msgs


def _m(i: int, account: str = "gmail_main") -> MailMessage:
    return MailMessage(
        account=account,
        message_id=f"<s{i}@t>",
        sender="x@greenhouse.io",
        subject=f"SUBJECT-{i} Acme Secret",
        received_at=AT - timedelta(days=i),
        body="We received your application.",
    )


def _received(company: str = "Acme") -> MailClassOut:
    return MailClassOut(
        kind="application_received",
        company=company,
        role="AI Engineer",
        next_step=None,
        deadline=None,
        confidence=0.9,
    )


def test_sync_dedups_and_isolates_account_failures(
    deps: Deps, fake: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    st = InMemoryAppStore()
    fake.script(MailClassOut, _received())
    good = ListFetcher([_m(1), _m(2)])
    fetchers: dict[str, Any] = {
        "gmail_main": good,
        "gmail_alt": ListFetcher(MailAuthExpired("gmail_alt: token expired, run auth")),
        "workspace": None,
        "yahoo": ListFetcher(RuntimeError("Acme Secret network detail")),
    }
    with caplog.at_level(logging.INFO):
        rep = run_mail_sync(deps, st, CFG, fetchers=fetchers, at=AT)
    a = rep.accounts
    assert (a["gmail_main"].fetched, a["gmail_main"].new, a["gmail_main"].created) == (2, 2, 1)
    assert a["gmail_main"].matched == 1  # second email matched the first application
    assert "token expired" in (a["gmail_alt"].error or "")
    assert a["workspace"].skipped == "not configured"
    assert a["yahoo"].error == "fetch failed: RuntimeError"
    assert rep.status == "partial" and st.sync_state()["gmail_alt"]["last_error"]
    assert "Secret" not in caplog.text and "SUBJECT" not in caplog.text  # counts only
    assert "Secret" not in json.dumps(st.runs[rep.run_id]["stats"])

    calls = len(fake.calls)
    rep2 = run_mail_sync(deps, st, CFG, fetchers={"gmail_main": good}, at=AT)
    assert rep2.accounts["gmail_main"].duplicates == 2 and len(fake.calls) == calls
    # The second run starts from the cursor minus the overlap window.
    assert good.since[-1] == AT - timedelta(days=CFG.overlap_days)


def test_cost_cap_stops_without_advancing_the_cursor(deps: Deps, fake: FakeProvider) -> None:
    st = InMemoryAppStore()
    deps.llm.ledger.record(UsageRecord("x", "n", "fake", "m", 1, 1, 999.0, 1))
    rep = run_mail_sync(
        deps,
        st,
        CFG,
        fetchers={"gmail_main": ListFetcher([_m(1)]), "yahoo": ListFetcher([_m(2, "yahoo")])},
        at=AT,
    )
    assert rep.status == "capped" and rep.stopped == "cost_cap"
    assert rep.accounts["yahoo"].skipped == "stopped"
    assert st.sync_state()["gmail_main"]["last_synced_at"] is None and st.events == {}


def test_backfill_window_and_ghosting(deps: Deps, fake: FakeProvider) -> None:
    st = InMemoryAppStore()
    fake.script(MailClassOut, _received())
    f = ListFetcher([_m(40)])  # 40 days old, nothing since
    rep = run_mail_sync(deps, st, CFG, days=90, fetchers={"gmail_main": f}, at=AT)
    assert f.since == [AT - timedelta(days=90)]
    assert rep.ghosted == 1 and st.list_apps()[0].status == "ghosted"
