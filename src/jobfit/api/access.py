"""Cloudflare Access verification at the origin.

Cloudflare Access puts a login in front of jobcheck.tycodes.app and adds a signed JWT to every
request it lets through (header `Cf-Access-Jwt-Assertion`, cookie `CF_Authorization`).
CONSTRAINT: the app must verify that JWT itself. Cloud Run also serves a public *.run.app URL that
bypasses Cloudflare entirely; without this check, anyone with that URL could start tailoring runs
on Tylor's paid Claude credits.

Off when CF_ACCESS_TEAM_DOMAIN / CF_ACCESS_AUD are unset (local dev). REQUIRE_ACCESS=1 (set on
Cloud Run) refuses to start without them, so a misconfigured deploy fails closed.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import cached_property
from typing import Any

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("jobfit.access")

# Cloud Run's health probes and uptime checks can't log in.
OPEN_PATHS = {"/api/health"}


class AccessVerifier:
    def __init__(
        self, team_domain: str, audience: str, allowed_emails: set[str] | None = None
    ) -> None:
        self.team_domain = team_domain.removeprefix("https://").rstrip("/")
        self.audience = audience
        self.allowed = {e.lower() for e in allowed_emails} if allowed_emails else None

    @cached_property
    def _jwks(self) -> jwt.PyJWKClient:
        # Keys rotate; PyJWKClient caches them and refetches when a token names an unknown kid.
        return jwt.PyJWKClient(f"https://{self.team_domain}/cdn-cgi/access/certs", cache_keys=True)

    def verify(self, token: str) -> dict[str, Any]:
        key = self._jwks.get_signing_key_from_jwt(token).key
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=self.audience,
            issuer=f"https://{self.team_domain}",
        )
        if self.allowed is not None and str(claims.get("email", "")).lower() not in self.allowed:
            raise jwt.InvalidTokenError("email not allowed")
        return claims

    async def middleware(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in OPEN_PATHS:
            return await call_next(request)
        token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get(
            "CF_Authorization"
        )
        if not token:
            return JSONResponse({"detail": "Sign in through jobcheck.tycodes.app"}, 403)
        try:
            request.state.user = self.verify(token).get("email")
        except (jwt.PyJWTError, jwt.PyJWKClientError) as e:
            log.warning("rejected Access token: %s", e)
            return JSONResponse({"detail": "Access token invalid or expired"}, 403)
        return await call_next(request)
