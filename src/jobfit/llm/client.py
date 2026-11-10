"""LLM client used by graph nodes: tier routing + cost cap + tracing + usage records.

    llm = build_llm()                       # from config/models.yaml and env
    res = llm.structured("score_fit", system, user, FitScore)

Depends on: jobfit.llm.base (Provider), jobfit.llm.cost (ledger), jobfit.tracing.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jobfit.config import ROOT, get_settings
from jobfit.llm.base import LLMError, LLMResult, Provider, QuotaExhausted, T, Tier, TierConfig
from jobfit.llm.cost import CostCapExceeded, CostLedger, InMemoryLedger, PgLedger, UsageRecord
from jobfit.tracing import MemoryTracer, current_ctx, get_tracer


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    provider: str
    daily_cost_cap_usd: float
    node_tiers: dict[str, Tier]
    tiers: dict[str, dict[str, TierConfig]]  # provider -> tier -> config
    fallbacks: tuple[str, ...] = ()  # tried in order when the primary can't answer
    eval_provider: str = "anthropic"  # evals pin one provider so recordings stay reproducible

    @classmethod
    def load(cls, path: Path | None = None) -> ModelsConfig:
        raw: dict[str, Any] = yaml.safe_load((path or ROOT / "config" / "models.yaml").read_text())
        tiers = {
            p: {t: TierConfig(**cfg) for t, cfg in spec["tiers"].items()}
            for p, spec in raw["providers"].items()
        }
        cap = float(os.getenv("DAILY_COST_CAP_USD", raw["daily_cost_cap_usd"]))
        # An empty LLM_PROVIDER (unset GitHub variable) means "use the config default".
        # Setting it pins that single provider: an explicit choice turns the fallback chain off.
        env = os.getenv("LLM_PROVIDER")
        provider = env or raw["provider"]
        fallbacks = () if env else tuple(raw.get("fallbacks") or ())
        return cls(
            provider,
            cap,
            raw["node_tiers"],
            tiers,
            fallbacks,
            raw.get("eval_provider", "anthropic"),
        )


log = logging.getLogger("jobfit.llm")

BENCH_SECONDS = 3600
# provider name -> monotonic time it may be tried again (process-wide: every LLM shares it)
_benched_until: dict[str, float] = {}


@dataclass
class LLM:
    provider: Provider
    config: ModelsConfig
    ledger: CostLedger = field(default_factory=InMemoryLedger)
    tracer: MemoryTracer = field(default_factory=MemoryTracer)
    run_id: str = "adhoc"
    fallbacks: list[Provider] = field(default_factory=list)

    def tier_for(self, node: str, provider: Provider | None = None) -> TierConfig:
        tier = self.config.node_tiers.get(node, "fast")
        name = (provider or self.provider).name
        # The fake provider reuses whichever real provider's tier table is configured.
        table = self.config.tiers.get(name) or self.config.tiers[self.config.provider]
        return table[tier]

    def chain(self) -> list[Provider]:
        """Providers to try for the next call: primary first, skipping any still benched."""
        now = time.monotonic()
        live = [
            p for p in [self.provider, *self.fallbacks] if _benched_until.get(p.name, 0.0) <= now
        ]
        return live or [self.provider]

    def check_cap(self) -> None:
        spent = self.ledger.spent_today()
        if spent >= self.config.daily_cost_cap_usd:
            raise CostCapExceeded(spent, self.config.daily_cost_cap_usd)

    def structured(self, node: str, system: str, user: str, schema: type[T]) -> LLMResult[T]:
        self.check_cap()
        chain = self.chain()
        last: LLMError | None = None
        for i, provider in enumerate(chain):
            tier = self.tier_for(node, provider)
            try:
                return self._call(node, provider, tier, system, user, schema)
            except QuotaExhausted as e:
                # WHY bench for an hour: Groq's daily quota refills gradually (seen in practice),
                # so retrying every call would just add a failed request in front of each one.
                _benched_until[provider.name] = time.monotonic() + BENCH_SECONDS
                log.warning(
                    "%s quota exhausted; benched %ss: %s",
                    provider.name,
                    BENCH_SECONDS,
                    str(e)[:160],
                )
                last = e
            except LLMError as e:
                log.warning("%s failed for %s: %s", provider.name, node, str(e)[:160])
                last = e
            if i < len(chain) - 1:
                log.info(
                    "falling back from %s to %s for %s", provider.name, chain[i + 1].name, node
                )
        assert last is not None
        raise last

    def _call(
        self,
        node: str,
        provider: Provider,
        tier: TierConfig,
        system: str,
        user: str,
        schema: type[T],
    ) -> LLMResult[T]:
        with self.tracer.observe(
            f"{node}.llm", kind="generation", input=user[:4000], model=tier.model
        ) as gen:
            t0 = time.perf_counter()
            out, usage, model = provider.structured(
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
                provider=provider.name,
            )
        ctx = current_ctx.get()
        self.ledger.record(
            UsageRecord(
                run_id=ctx.trace_id if ctx else self.run_id,
                node=node,
                provider=provider.name,
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


def has_key(name: str) -> bool:
    s = get_settings()
    if name == "groq":
        return bool(s.groq_api_key)
    if name == "anthropic":
        return bool(s.anthropic_api_key or os.getenv("ANTHROPIC_AUTH_TOKEN"))
    return False


def build_llm(provider: Provider | None = None) -> LLM:
    cfg = ModelsConfig.load()
    dsn = get_settings().database_url
    ledger: CostLedger = PgLedger(dsn) if dsn else InMemoryLedger()
    if provider is not None:
        return LLM(provider, cfg, ledger, get_tracer())
    # Providers without a key are left out, so a missing Groq key just means "Claude only".
    names = [n for n in (cfg.provider, *cfg.fallbacks) if has_key(n)] or [cfg.provider]
    built = [build_provider(n) for n in dict.fromkeys(names)]
    return LLM(built[0], cfg, ledger, get_tracer(), fallbacks=built[1:])
