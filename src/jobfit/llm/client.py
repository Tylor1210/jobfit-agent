"""LLM client used by graph nodes: tier routing + cost cap + tracing + usage records.

    llm = build_llm()                       # from config/models.yaml and env
    res = llm.structured("score_fit", system, user, FitScore)

Depends on: jobfit.llm.base (Provider), jobfit.llm.cost (ledger), jobfit.tracing.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jobfit.config import ROOT, get_settings
from jobfit.llm.base import LLMResult, Provider, T, Tier, TierConfig
from jobfit.llm.cost import CostCapExceeded, CostLedger, InMemoryLedger, PgLedger, UsageRecord
from jobfit.tracing import MemoryTracer, current_ctx, get_tracer


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    provider: str
    daily_cost_cap_usd: float
    node_tiers: dict[str, Tier]
    tiers: dict[str, dict[str, TierConfig]]  # provider -> tier -> config

    @classmethod
    def load(cls, path: Path | None = None) -> ModelsConfig:
        raw: dict[str, Any] = yaml.safe_load((path or ROOT / "config" / "models.yaml").read_text())
        tiers = {
            p: {t: TierConfig(**cfg) for t, cfg in spec["tiers"].items()}
            for p, spec in raw["providers"].items()
        }
        cap = float(os.getenv("DAILY_COST_CAP_USD", raw["daily_cost_cap_usd"]))
        return cls(os.getenv("LLM_PROVIDER", raw["provider"]), cap, raw["node_tiers"], tiers)


@dataclass
class LLM:
    provider: Provider
    config: ModelsConfig
    ledger: CostLedger = field(default_factory=InMemoryLedger)
    tracer: MemoryTracer = field(default_factory=MemoryTracer)
    run_id: str = "adhoc"

    def tier_for(self, node: str) -> TierConfig:
        tier = self.config.node_tiers.get(node, "fast")
        # The fake provider reuses whichever real provider's tier table is configured.
        table = self.config.tiers.get(self.provider.name) or self.config.tiers[self.config.provider]
        return table[tier]

    def check_cap(self) -> None:
        spent = self.ledger.spent_today()
        if spent >= self.config.daily_cost_cap_usd:
            raise CostCapExceeded(spent, self.config.daily_cost_cap_usd)

    def structured(self, node: str, system: str, user: str, schema: type[T]) -> LLMResult[T]:
        self.check_cap()
        tier = self.tier_for(node)
        with self.tracer.observe(
            f"{node}.llm", kind="generation", input=user[:4000], model=tier.model
        ) as gen:
            t0 = time.perf_counter()
            out, usage, model = self.provider.structured(
                tier=tier, system=system, user=user, schema=schema
            )
            latency = int((time.perf_counter() - t0) * 1000)
            cost = usage.cost_usd(tier)
            gen.update(
                output=out.model_dump(mode="json"),
                usage_details={
                    "input": usage.input_tokens,
                    "output": usage.output_tokens,
                    "cache_read_input_tokens": usage.cache_read_tokens,
                },
                cost_details={"total": cost},
                latency_ms=latency,
            )
        ctx = current_ctx.get()
        self.ledger.record(
            UsageRecord(
                run_id=ctx.trace_id if ctx else self.run_id,
                node=node,
                provider=self.provider.name,
                model=model,
                input_tokens=usage.input_tokens + usage.cache_read_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=cost,
                latency_ms=latency,
            )
        )
        return LLMResult(out, usage, model, latency, cost)


def build_provider(name: str) -> Provider:
    s = get_settings()
    if name == "anthropic":
        import anthropic

        from jobfit.llm.anthropic_provider import AnthropicProvider

        # No key in .env: let the SDK resolve ANTHROPIC_API_KEY or an `ant auth login` profile.
        client = (
            anthropic.Anthropic(api_key=s.anthropic_api_key, max_retries=4)
            if s.anthropic_api_key
            else None
        )
        return AnthropicProvider(client)
    if name == "groq":
        import groq

        from jobfit.llm.groq_provider import MAX_RETRIES, GroqProvider

        key = s.groq_api_key
        return GroqProvider(groq.Groq(api_key=key, max_retries=MAX_RETRIES) if key else None)
    raise ValueError(f"unknown LLM provider {name!r} (expected anthropic or groq)")


def build_llm(provider: Provider | None = None) -> LLM:
    cfg = ModelsConfig.load()
    dsn = get_settings().database_url
    ledger: CostLedger = PgLedger(dsn) if dsn else InMemoryLedger()
    return LLM(provider or build_provider(cfg.provider), cfg, ledger, get_tracer())
