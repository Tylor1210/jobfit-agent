"""Read-only Gmail connector (Gmail REST API over httpx, scope gmail.readonly).

Used by mail/sync.py. Filters server-side with a Gmail search query, so only application-related
mail is downloaded. Bodies are returned in memory for classification and never stored.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx

from jobfit.applications.models import MailMessage
from jobfit.mail.config import MailAuthExpired, MailConfig

log = logging.getLogger("jobfit.mail.gmail")

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def build_query(cfg: MailConfig, since: datetime) -> str:
    senders = " OR ".join(cfg.senders)
    terms = " OR ".join(cfg.subject_terms)
    excl = " ".join(f"-from:{s}" for s in cfg.exclude_senders)
    # GOTCHA: `after:` with a unix timestamp is exact; a date would be read in the account's
    # own timezone.
    q = f"after:{int(since.timestamp())} (from:({senders}) OR subject:({terms}))"
    return f"{q} {excl} -category:promotions".replace("  ", " ").strip()


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def extract_body(payload: dict[str, Any]) -> str:
    """Prefer text/plain parts; fall back to text/html (the sanitizer strips tags)."""
    plain: list[str] = []
    html: list[str] = []

    def walk(p: dict[str, Any]) -> None:
        mime = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            html.append(_b64(data))
        for child in p.get("parts", []) or []:
            walk(child)

    walk(payload)
    return "\n".join(plain) if plain else "\n".join(html)


class GmailFetcher:
    def __init__(
        self,
        label: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        token_env: str,
        client: httpx.Client | None = None,
    ) -> None:
        self.label, self.token_env = label, token_env
        self._cid, self._secret, self._refresh = client_id, client_secret, refresh_token
        self._http = client or httpx.Client(timeout=30)

    def _access_token(self) -> str:
        r = self._http.post(
            TOKEN_URL,
            data={
                "client_id": self._cid,
                "client_secret": self._secret,
                "refresh_token": self._refresh,
                "grant_type": "refresh_token",
            },
        )
        if r.status_code in (400, 401):
            err = r.json().get("error", "") if r.content else ""
            if err in ("invalid_grant", "unauthorized_client", "invalid_client"):
                raise MailAuthExpired(
                    f"{self.label}: Google refused the refresh token ({err}). It expired or was "
                    f"revoked. Run `uv run jobfit mail auth {self.label}`, then update the "
                    f"{self.token_env} GitHub secret."
                )
        r.raise_for_status()
        return str(r.json()["access_token"])

    def fetch(self, cfg: MailConfig, since: datetime) -> Iterator[MailMessage]:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        params: dict[str, Any] = {"q": build_query(cfg, since), "maxResults": 100}
        ids: list[str] = []
        while len(ids) < cfg.max_messages_per_account:
            r = self._http.get(f"{API}/messages", headers=headers, params=params)
            r.raise_for_status()
            body = r.json()
            ids += [m["id"] for m in body.get("messages", [])]
            if not body.get("nextPageToken"):
                break
            params["pageToken"] = body["nextPageToken"]
        for mid in ids[: cfg.max_messages_per_account]:
            r = self._http.get(f"{API}/messages/{mid}", headers=headers, params={"format": "full"})
            r.raise_for_status()
            yield self._parse(r.json())

    def _parse(self, m: dict[str, Any]) -> MailMessage:
        payload = m.get("payload", {})
        hdr = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        return MailMessage(
            account=self.label,
            # WHY the RFC Message-ID: the same email in two inboxes (CC, forward) is one event.
            message_id=hdr.get("message-id") or f"gmail:{m['id']}",
            sender=hdr.get("from", ""),
            subject=hdr.get("subject", ""),
            received_at=datetime.fromtimestamp(int(m.get("internalDate", 0)) / 1000, UTC),
            body=extract_body(payload),
        )
