"""App settings from env vars / .env. One place for paths, model names, and limits.

CONSTRAINT: no model names or secrets hardcoded elsewhere. Read them from here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    profile_dir: Path = ROOT / "profile"
    database_url: str | None = None

    # WHY: bge-small is 384-dim, ~130MB, runs on CPU via ONNX (fastembed). Free and fast enough
    # for a few hundred chunks/postings a day. Changing it requires re-indexing (dim may change).
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    # Provider keys. Read from .env here (the SDKs only see real env vars, not .env).
    # WHY: AliasChoices accepts the spelling already in Tylor's .env as well as the standard one.
    anthropic_api_key: str | None = None
    groq_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("GROQ_API_KEY", "GROQ_APIKEY")
    )
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_base_url: str = "https://cloud.langfuse.com"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
