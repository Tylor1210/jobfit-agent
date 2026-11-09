"""Shared plumbing for both graphs: dependencies bundle, traced node wrapper, error routing.

Every node is wrapped so that it (1) gets its own tracing span under the run's trace,
(2) turns a cost-cap hit or provider failure into `state["error"]` instead of an exception, and
(3) the graph routes any error straight to END. That is what "stops cleanly" means.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jobfit.llm.base import LLMError
from jobfit.llm.client import LLM
from jobfit.llm.cost import CostCapExceeded
from jobfit.profile.constraints import Constraints
from jobfit.profile.models import Profile
from jobfit.rag.store import VectorStore
from jobfit.schemas import ResumeOut
from jobfit.store.repo import Repo
from jobfit.tracing import TraceCtx, current_ctx

log = logging.getLogger("jobfit.graph")

State = dict[str, Any]
NodeFn = Callable[[State], State]


@dataclass
class Deps:
    llm: LLM
    store: VectorStore
    repo: Repo
    profile: Profile
    constraints: Constraints
    # Renders a resume and returns its page count. None skips the page check (unit tests).
    page_count: Callable[[ResumeOut], int] | None = None
    # Renders both PDFs; returns (resume_path, cover_path). Set by build_deps.
    render: Callable[..., tuple[str, str]] | None = None
    verify_with_llm: bool = True
    voice_samples: list[str] = field(default_factory=list)


def node(name: str, deps: Deps) -> Callable[[NodeFn], NodeFn]:
    def deco(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        def wrapped(state: State) -> State:
            # Re-enter the run's trace on whatever thread LangGraph runs this node on.
            if current_ctx.get() is None and state.get("trace_id"):
                current_ctx.set(TraceCtx(state["trace_id"], state.get("trace_root")))
            with deps.llm.tracer.observe(name, kind="span") as span:
                try:
                    out = fn(state)
                except CostCapExceeded as e:
                    log.warning("%s stopped: %s", name, e)
                    span.update(level="WARNING", stopped="cost_cap")
                    return {"error": f"cost_cap: {e}", "error_node": name}
                except LLMError as e:
                    log.error("%s failed: %s", name, e)
                    span.update(level="ERROR", error=str(e))
                    return {"error": f"llm: {e}", "error_node": name}
                span.update(output={k: _brief(v) for k, v in out.items()})
                return out

        return wrapped

    return deco


def _brief(v: Any) -> Any:
    """Keep span payloads small: summarize big values instead of shipping them whole."""
    if isinstance(v, list):
        return f"list[{len(v)}]"
    if isinstance(v, str) and len(v) > 300:
        return v[:300] + "..."
    if hasattr(v, "model_dump"):
        return type(v).__name__
    return v


def stop_on_error(next_node: str) -> Callable[[State], str]:
    def route(state: State) -> str:
        return "__end__" if state.get("error") else next_node

    return route
