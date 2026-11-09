"""Scripted provider for tests and offline evals. No network, deterministic, still reports usage
so the cost cap and tracing paths are exercised exactly as in production.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from jobfit.llm.base import T, TierConfig, Usage

Responder = Callable[[str, str], BaseModel | dict[str, Any]]


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self._queues: dict[str, deque[BaseModel | dict[str, Any] | Responder]] = defaultdict(deque)
        self.calls: list[tuple[str, str, str]] = []  # (schema name, system, user)

    def script(
        self, schema: type[BaseModel], *responses: BaseModel | dict[str, Any] | Responder
    ) -> FakeProvider:
        """Queue responses for a schema. A callable gets (system, user) and returns the output.
        The last queued item repeats once the queue would otherwise run dry."""
        self._queues[schema.__name__].extend(responses)
        return self

    def structured(
        self, *, tier: TierConfig, system: str, user: str, schema: type[T]
    ) -> tuple[T, Usage, str]:
        self.calls.append((schema.__name__, system, user))
        q = self._queues[schema.__name__]
        if not q:
            raise AssertionError(f"FakeProvider has no scripted response for {schema.__name__}")
        item = q.popleft() if len(q) > 1 else q[0]
        out = item(system, user) if callable(item) else item
        parsed = schema.model_validate(out.model_dump() if isinstance(out, BaseModel) else out)
        # Rough token estimate (4 chars/token) so cost metrics are non-zero and comparable.
        usage = Usage(
            input_tokens=(len(system) + len(user)) // 4,
            output_tokens=len(parsed.model_dump_json()) // 4,
        )
        return parsed, usage, f"fake:{tier.model}"
