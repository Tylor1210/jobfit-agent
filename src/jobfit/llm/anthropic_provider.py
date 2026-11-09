"""Anthropic adapter. Structured output via messages.parse(output_format=PydanticModel)."""

from __future__ import annotations

from typing import Any

import anthropic

from jobfit.llm.base import LLMError, T, TierConfig, Usage


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        # Zero-arg client resolves ANTHROPIC_API_KEY or an `ant auth login` profile.
        self._client = client or anthropic.Anthropic(max_retries=4)

    def structured(
        self, *, tier: TierConfig, system: str, user: str, schema: type[T]
    ) -> tuple[T, Usage, str]:
        kwargs: dict[str, Any] = {
            "model": tier.model,
            "max_tokens": tier.max_tokens,
            # WHY: system prompts are frozen per node, so caching them is free when they are long
            # enough to qualify and harmless when they are not.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_format": schema,
        }
        if tier.effort:
            kwargs["output_config"] = {"effort": tier.effort}
        if tier.fallbacks:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = tier.fallbacks
        try:
            resp = self._client.beta.messages.parse(**kwargs)
        except anthropic.BadRequestError as e:
            raise LLMError(f"anthropic rejected request: {e.message}") from e
        except anthropic.AuthenticationError as e:
            raise LLMError("anthropic auth failed: set ANTHROPIC_API_KEY") from e
        # Rate limits, 5xx and connection errors were already retried by the SDK; what reaches
        # here is final, so stop the run cleanly instead of crashing it.
        except anthropic.RateLimitError as e:
            raise LLMError(f"anthropic rate limit: {e.message}") from e
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            raise LLMError(f"anthropic request failed: {e}") from e

        if resp.stop_reason == "refusal":
            raise LLMError("model refused the request")
        if resp.stop_reason == "max_tokens":
            raise LLMError(f"hit max_tokens={tier.max_tokens}; raise it in config/models.yaml")
        parsed = resp.parsed_output
        if parsed is None:
            raise LLMError("no parsed output returned")
        u = resp.usage
        usage = Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=u.cache_read_input_tokens or 0,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
        )
        return parsed, usage, resp.model
