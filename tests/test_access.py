"""Cloudflare Access verification at the origin (offline: local RSA key, patched JWKS)."""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobfit.api.access import AccessVerifier

TEAM = "tycodes.cloudflareaccess.com"
AUD = "aud-123"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key: Any = KEY, **over: Any) -> str:
    claims = {
        "aud": [AUD],
        "iss": f"https://{TEAM}",
        "email": "tylor@tycodes.dev",
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    claims.update(over)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    v = AccessVerifier(TEAM, AUD, {"tylor@tycodes.dev"})

    class Jwks:
        def get_signing_key_from_jwt(self, _t: str) -> Any:
            return type("K", (), {"key": KEY.public_key()})()

    monkeypatch.setattr(AccessVerifier, "_jwks", Jwks())
    app = FastAPI()
    app.middleware("http")(v.middleware)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/jobs")
    def jobs() -> list[str]:
        return ["x"]

    return TestClient(app)


def test_health_is_open_everything_else_needs_a_token(client: TestClient) -> None:
    assert client.get("/api/health").status_code == 200
    r = client.get("/api/jobs")
    assert r.status_code == 403 and "Sign in" in r.json()["detail"]


def test_valid_token_header_or_cookie(client: TestClient) -> None:
    assert client.get("/api/jobs", headers={"Cf-Access-Jwt-Assertion": token()}).status_code == 200
    client.cookies.set("CF_Authorization", token())
    assert client.get("/api/jobs").status_code == 200


@pytest.mark.parametrize(
    "bad",
    [
        {"key": OTHER},  # forged signature
        {"aud": ["someone-elses-app"]},  # token for a different Access app
        {"iss": "https://evil.cloudflareaccess.com"},
        {"exp": int(time.time()) - 10},  # expired
        {"email": "stranger@example.com"},  # not on the allow-list
    ],
)
def test_bad_tokens_rejected(client: TestClient, bad: dict[str, Any]) -> None:
    key = bad.pop("key", KEY)
    r = client.get("/api/jobs", headers={"Cf-Access-Jwt-Assertion": token(key, **bad)})
    assert r.status_code == 403


def test_require_access_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from jobfit.api import app as app_module
    from jobfit.config import get_settings

    monkeypatch.setenv("REQUIRE_ACCESS", "1")
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="REQUIRE_ACCESS"):
            app_module.create_app(
                deps=object(),
                dashboard=object(),  # type: ignore[arg-type]
                checkpointer=object(),
                serve_web=False,
            )
    finally:
        get_settings.cache_clear()
