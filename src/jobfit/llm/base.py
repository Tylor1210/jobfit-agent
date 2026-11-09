"""The LLM "socket": one interface every provider adapter implements.

Graph nodes never talk to a vendor SDK. They call jobfit.llm.client.LLM, which picks a provider
adapter (anthropic_provider, groq_provider, or fake for tests), enforces the cost cap, and traces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
Tier = Literal["fast", "smart"]


@dataclass(frozen=True, slots=True)
class TierConfig:
    model: str
    input_per_mtok: float
    output_per_mtok: float
    max_tokens: int = 8000
    effort: str | None = None
    fallbacks: str | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def cost_usd(self, t: TierConfig) -> float:
        # Cache reads bill at ~0.1x input and cache writes at ~1.25x input on Anthropic.
        per_in = t.input_per_mtok / 1e6
        return (
            self.input_tokens * per_in
            + self.cache_read_tokens * per_in * 0.1
            + self.cache_write_tokens * per_in * 1.25
            + self.output_tokens * t.output_per_mtok / 1e6
        )


@dataclass(frozen=True, slots=True)
class LLMResult[M: BaseModel]:
    output: M
    usage: Usage
    model: str
    latency_ms: int
    cost_usd: float


class LLMError(RuntimeError):
    """Provider call failed in a way retrying the same request will not fix."""


class Provider(Protocol):
    name: str

    def structured(
        self, *, tier: TierConfig, system: str, user: str, schema: type[T]
    ) -> tuple[T, Usage, str]:
        """Return (validated output, usage, model id that actually served the call)."""
        ...
