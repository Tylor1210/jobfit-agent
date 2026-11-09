"""Groq adapter (free tier). JSON-schema response format, validated with Pydantic.

GOTCHA: not every Groq model supports response_format=json_schema. On a 400 we fall back to
json_object mode with the schema in the prompt. Either way Pydantic validates the result, and one
repair attempt is made with the validation error before giving up.

GOTCHA: the free tier allows ~8k tokens/minute per model, and one drafting call is ~4k. The client
retries 429s honoring retry-after (max_retries below), so a tailor run slows down instead of
failing. Only a 429 that survives every retry becomes an LLMError (the run stops cleanly).
"""

from __future__ import annotations

import json
from typing import Any

import groq
from pydantic import ValidationError

from jobfit.llm.base import LLMError, T, TierConfig, Usage

MAX_RETRIES = 6


class GroqProvider:
    name = "groq"

    def __init__(self, client: groq.Groq | None = None) -> None:
        self._client = client or groq.Groq(max_retries=MAX_RETRIES)
        self._schema_mode_ok: dict[str, bool] = {}

    def _call(
        self, tier: TierConfig, messages: list[dict[str, str]], schema: type[T]
    ) -> tuple[str, Usage, str]:
        use_schema = self._schema_mode_ok.get(tier.model, True)
        fmt: dict[str, Any] = (
            {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
            }
            if use_schema
            else {"type": "json_object"}
        )
        try:
            resp = self._client.chat.completions.create(  # type: ignore[call-overload]
                model=tier.model,
                messages=messages,
                response_format=fmt,
                max_tokens=tier.max_tokens,
            )
        except groq.BadRequestError as e:
            if not use_schema:
                raise LLMError(f"groq rejected request: {e}") from e
            self._schema_mode_ok[tier.model] = False
            schema_note = "\nJSON Schema:\n" + json.dumps(
                schema.model_json_schema(), separators=(",", ":")
            )
            messages = [
                {**messages[0], "content": messages[0]["content"] + schema_note},
                *messages[1:],
            ]
            return self._call(tier, messages, schema)
        except groq.AuthenticationError as e:
            raise LLMError("groq auth failed: set GROQ_API_KEY") from e
        except groq.RateLimitError as e:
            raise LLMError(f"groq rate limit after {MAX_RETRIES} retries: {e.message}") from e
        except (groq.APIStatusError, groq.APIConnectionError) as e:
            raise LLMError(f"groq request failed: {e}") from e
        u = resp.usage
        usage = Usage(
            input_tokens=u.prompt_tokens if u else 0, output_tokens=u.completion_tokens if u else 0
        )
        return resp.choices[0].message.content or "", usage, resp.model

    def structured(
        self, *, tier: TierConfig, system: str, user: str, schema: type[T]
    ) -> tuple[T, Usage, str]:
        if self._schema_mode_ok.get(tier.model, True):
            # WHY: response_format already carries the schema; repeating it costs ~1k tokens.
            sys = f"{system}\n\nRespond with one JSON object."
        else:
            sys = (
                f"{system}\n\nRespond with one JSON object matching this JSON Schema:\n"
                f"{json.dumps(schema.model_json_schema(), separators=(',', ':'))}"
            )
        messages = [{"role": "system", "content": sys}, {"role": "user", "content": user}]
        total = Usage()
        for attempt in range(2):
            text, usage, model = self._call(tier, messages, schema)
            total = Usage(
                total.input_tokens + usage.input_tokens, total.output_tokens + usage.output_tokens
            )
            try:
                return schema.model_validate_json(text), total, model
            except ValidationError as e:
                if attempt == 1:
                    raise LLMError(f"groq output failed validation twice: {e}") from e
                messages += [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": f"That JSON is invalid: {e}. Return corrected JSON only.",
                    },
                ]
        raise AssertionError("unreachable")
