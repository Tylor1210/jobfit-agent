"""Read-only IMAP connector (Yahoo, app password). Used by mail/sync.py.

CONSTRAINT: read-only. Folders are opened with EXAMINE (`select(readonly=True)`) and messages are
fetched with BODY.PEEK[], so nothing is marked read, moved, flagged or deleted.
"""

from __future__ import annotations

import contextlib
import email
import imaplib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import parsedate_to_datetime

from jobfit.applications.models import MailMessage
from jobfit.mail.config import MailAuthExpired, MailConfig

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _q(s: str) -> str:
    return '"' + s.replace("\\", "").replace('"', "") + '"'


def _or_chain(keys: list[str]) -> str:
    # IMAP OR takes exactly two keys; nest to the right: OR a OR b c
    return keys[0] if len(keys) == 1 else f"OR {keys[0]} {_or_chain(keys[1:])}"


def build_search(cfg: MailConfig, since: datetime) -> str:
    d = f"{since.day:02d}-{_MONTHS[since.month - 1]}-{since.year}"
    keys = [f"FROM {_q(s)}" for s in cfg.senders] + [f"SUBJECT {_q(t)}" for t in cfg.subject_terms]
    excl = "".join(f" NOT FROM {_q(s)}" for s in cfg.exclude_senders)
    return f"SINCE {d} ({_or_chain(keys)}){excl}"


def _body(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    try:
        return str(part.get_content()) if part else ""
    except (LookupError, ValueError):  # unknown charset
        payload = part.get_payload(decode=True) if part else b""
        return payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""


class ImapFetcher:
    def __init__(
        self,
        label: str,
        host: str,
        user: str,
        password: str,
        folders: list[str],
        password_env: str,
        connect: Callable[[str], imaplib.IMAP4] | None = None,
    ) -> None:
        self.label, self.host, self.folders, self.password_env = label, host, folders, password_env
        self._user, self._pw = user, password
        self._connect = connect or (lambda h: imaplib.IMAP4_SSL(h, timeout=30))

    def fetch(self, cfg: MailConfig, since: datetime) -> Iterator[MailMessage]:
        conn = self._connect(self.host)
        try:
            try:
                conn.login(self._user, self._pw)
            except imaplib.IMAP4.error as e:
                raise MailAuthExpired(
                    f"{self.label}: IMAP login failed ({str(e)[:80]}). The app password was "
                    f"revoked or mistyped. Generate a new one and update {self.password_env} "
                    f"in .env and GitHub secrets."
                ) from e
            n = 0
            for folder in self.folders:
                typ, _ = conn.select(folder, readonly=True)  # EXAMINE: read-only
                if typ != "OK":
                    continue
                typ, data = conn.uid("SEARCH", build_search(cfg, since))
                uids = data[0].split() if typ == "OK" and data and data[0] else []
                for uid in uids[-cfg.max_messages_per_account :]:
                    if n >= cfg.max_messages_per_account:
                        return
                    typ, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                    raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                    if typ != "OK" or not isinstance(raw, bytes):
                        continue
                    n += 1
                    yield self._parse(raw, folder, uid)
        finally:
            with contextlib.suppress(Exception):
                conn.logout()

    def _parse(self, raw: bytes, folder: str, uid: bytes) -> MailMessage:
        msg = email.message_from_bytes(raw, policy=default_policy)
        assert isinstance(msg, EmailMessage)
        try:
            when = parsedate_to_datetime(str(msg["Date"]))
            when = when if when.tzinfo else when.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            when = datetime.now(UTC)
        return MailMessage(
            account=self.label,
            message_id=str(msg["Message-ID"] or f"imap:{self.label}:{folder}:{uid.decode()}"),
            sender=str(msg["From"] or ""),
            subject=str(msg["Subject"] or ""),
            received_at=when,
            body=_body(msg),
        )
