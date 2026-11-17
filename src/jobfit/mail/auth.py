"""`jobfit mail auth <label>`: one-time Google OAuth (loopback + PKCE) for a Gmail account.

Opens the consent page in a browser, catches the redirect on 127.0.0.1, exchanges the code, and
writes the refresh token into .env under the account's env var name. The token is never printed.

WHY no google-auth-oauthlib: the flow is ~40 lines over httpx, already a dependency.
GOTCHA: an OAuth app in "Testing" status issues refresh tokens that expire after 7 days. Set the
consent screen to "In production" (no verification needed for personal use); see MAIL_SETUP.md.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import re
import secrets
import socket
import threading
import urllib.parse
import webbrowser

import httpx

from jobfit.mail.config import ENV_FILE, MailConfig, secret
from jobfit.mail.gmail import SCOPE, TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def write_env(name: str, value: str, path: str | None = None) -> None:
    """Set NAME=value in .env, replacing an existing line."""
    from pathlib import Path

    p = Path(path) if path else ENV_FILE
    text = p.read_text() if p.exists() else ""
    line = f"{name}={value}"
    if re.search(rf"^{re.escape(name)}=.*$", text, flags=re.M):
        text = re.sub(rf"^{re.escape(name)}=.*$", lambda _: line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + ("\n" if text else "") + line + "\n"
    p.write_text(text)


def authorize(label: str, cfg: MailConfig | None = None, timeout_s: int = 300) -> str:
    cfg = cfg or MailConfig.load()
    acct = cfg.accounts.get(label)
    if not acct or acct.type != "gmail" or not acct.refresh_token_env:
        raise SystemExit(f"{label} is not a gmail account in config/mail.yaml")
    cid, csecret = secret(cfg.google.client_id_env), secret(cfg.google.client_secret_env)
    if not (cid and csecret):
        raise SystemExit(
            f"Set {cfg.google.client_id_env} and {cfg.google.client_secret_env} in .env first "
            "(MAIL_SETUP.md, step 1)."
        )
    port = _free_port()
    redirect = f"http://127.0.0.1:{port}"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    state = secrets.token_urlsafe(16)
    url = (
        AUTH_URL
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": cid,
                "redirect_uri": redirect,
                "response_type": "code",
                "scope": SCOPE,
                "access_type": "offline",
                "prompt": "consent",  # WHY: Google only returns a refresh token on consent
                "state": state,
                "code_challenge": challenge.decode(),
                "code_challenge_method": "S256",
            }
        )
    )
    got: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and return to the terminal.")

        def log_message(self, *_: object) -> None:  # keep the code out of the terminal
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = timeout_s
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()
    print(f"Opening Google sign-in for {label}. Pick the account that belongs to this label.")
    print(f"If no browser opens, visit:\n{url}\n")
    webbrowser.open(url)
    t.join(timeout_s)
    server.server_close()
    if got.get("state") != state or "code" not in got:
        raise SystemExit(f"authorization failed or timed out: {got.get('error', 'no code')}")
    r = httpx.post(
        TOKEN_URL,
        data={
            "code": got["code"],
            "client_id": cid,
            "client_secret": csecret,
            "redirect_uri": redirect,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
        timeout=30,
    )
    r.raise_for_status()
    token = r.json().get("refresh_token")
    if not token:
        raise SystemExit(
            "Google returned no refresh token. Remove the app's access at "
            "myaccount.google.com/permissions and run this again."
        )
    write_env(acct.refresh_token_env, token)
    return acct.refresh_token_env
