"""Typed loaders for config/ingest.yaml and config/watchlist.yaml, plus source API keys."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from jobfit.config import ROOT


class Board(BaseModel):
    token: str
    name: str | None = None


class Watchlist(BaseModel):
    greenhouse: list[Board] = Field(default_factory=list)
    lever: list[Board] = Field(default_factory=list)
    ashby: list[Board] = Field(default_factory=list)


class Prefilter(BaseModel):
    title_include: list[str]
    title_exclude: list[str] = Field(default_factory=list)
    local_places: list[str] = Field(default_factory=list)
    remote_reject: list[str] = Field(default_factory=list)
    min_similarity: float = 0.55
    local_bonus: float = 0.08


class IngestConfig(BaseModel):
    sources: dict[str, dict[str, Any]]
    stale_days: int = 7
    prefilter: Prefilter
    score_top_n: int = 15
    rescore_per_run: int = 10

    def enabled(self, name: str) -> bool:
        return bool(self.sources.get(name, {}).get("enabled", False))

    def opts(self, name: str) -> dict[str, Any]:
        return self.sources.get(name, {})


class SourceKeys(BaseSettings):
    """Keys for sources that need them. Missing keys disable that source, they never fail a run."""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    usajobs_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("USAJOBS_API_KEY", "USAJOBS_APIKEY")
    )
    # USAJOBS requires the email you registered the key with as the User-Agent.
    usajobs_email: str | None = None
    adzuna_app_id: str | None = None
    adzuna_app_key: str | None = None


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_ingest_config(path: Path | None = None) -> IngestConfig:
    return IngestConfig.model_validate(_load(path or ROOT / "config" / "ingest.yaml"))


@lru_cache(maxsize=1)
def load_watchlist(path: Path | None = None) -> Watchlist:
    return Watchlist.model_validate(_load(path or ROOT / "config" / "watchlist.yaml") or {})
