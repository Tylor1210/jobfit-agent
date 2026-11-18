"""Mail config (config/mail.yaml) and secret lookup. Used by the connectors, auth, and sync.

CONSTRAINT: secrets are read by env var NAME from the environment or .env, never from the YAML.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel

from jobfit.config import ROOT

MAIL_CONFIG = ROOT / "config" / "mail.yaml"
ENV_FILE = ROOT / ".env"


class GoogleCfg(BaseModel):
    client_id_env: str
    client_secret_env: str


class AccountCfg(BaseModel):
    type: Literal["gmail", "imap"]
    refresh_token_env: str | None = None  # gmail
    host: str | None = None  # imap
    user_env: str | None = None
    password_env: str | None = None
    folders: list[str] = ["INBOX"]


class MailConfig(BaseModel):
    lookback_days: int = 90
    overlap_days: int = 2
    max_messages_per_account: int = 400
    senders: list[str]
    subject_terms: list[str]
    exclude_senders: list[str] = []
    google: GoogleCfg
    accounts: dict[str, AccountCfg]

    @classmethod
    def load(cls, path: Path = MAIL_CONFIG) -> MailConfig:
        return cls.model_validate(yaml.safe_load(path.read_text()))


@lru_cache(maxsize=1)
def _dotenv() -> dict[str, str | None]:
    return dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}


def secret(name: str | None) -> str | None:
    """Env var first (CI), then .env (local). Blank values count as missing."""
    if not name:
        return None
    v = os.environ.get(name) or _dotenv().get(name) or ""
    return v.strip() or None


class MailAuthExpired(RuntimeError):
    """A refresh token or app password stopped working. The message says how to fix it."""
