"""Tracing: one trace per graph run, one span per node, one generation per LLM call.

Langfuse (free cloud tier) when LANGFUSE_PUBLIC_KEY/SECRET_KEY are set, otherwise a no-op tracer
that still records spans in memory (tests assert on them).

GOTCHA: LangGraph may run nodes on worker threads, so we do not rely on OpenTelemetry's implicit
context. The run's trace id and the current node span id travel in a contextvar that each node
wrapper sets on its own thread, and every observation is created with an explicit trace_context.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class TraceCtx:
    trace_id: str
    parent_id: str | None = None


current_ctx: contextvars.ContextVar[TraceCtx | None] = contextvars.ContextVar(
    "jobfit_trace_ctx", default=None
)


@dataclass(slots=True)
class Observation:
    name: str
    kind: str
    trace_id: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_id: str | None = None
    output: Any = None
    level: str = "DEFAULT"
    meta: dict[str, Any] = field(default_factory=dict)
    _lf: Any = None  # underlying Langfuse observation

    def update(self, *, output: Any = None, level: str | None = None, **meta: Any) -> None:
        if output is not None:
            self.output = output
        if level:
            self.level = level
        self.meta.update(meta)
        if self._lf is not None:
            kw: dict[str, Any] = {}
            if output is not None:
                kw["output"] = output
            if level:
                kw["level"] = level
            for k in ("usage_details", "cost_details", "model"):
                if k in meta:
                    kw[k] = meta[k]
            rest = {k: v for k, v in meta.items() if k not in kw}
            if rest:
                kw["metadata"] = rest
            self._lf.update(**kw)


def _is_control_flow(e: BaseException) -> bool:
    # GOTCHA: LangGraph's interrupt() works by raising; that pause is not an error.
    return type(e).__name__ in {"GraphInterrupt", "NodeInterrupt", "ParentCommand"}


class Tracer(Protocol):
    def run(self, name: str, input: Any, **meta: Any) -> Any: ...
    def observe(self, name: str, kind: str = "span", input: Any = None, **meta: Any) -> Any: ...
    def url(self, trace_id: str) -> str | None: ...
    def flush(self) -> None: ...


class MemoryTracer:
    """Default tracer. Keeps observations in memory; useful in tests and offline evals."""

    def __init__(self) -> None:
        self.observations: list[Observation] = []

    @contextmanager
    def run(self, name: str, input: Any, **meta: Any) -> Iterator[Observation]:
        trace_id = uuid.uuid4().hex
        obs = Observation(name, "trace", trace_id, meta=dict(meta))
        self.observations.append(obs)
        token = current_ctx.set(TraceCtx(trace_id, obs.id))
        try:
            yield obs
        finally:
            current_ctx.reset(token)

    @contextmanager
    def observe(
        self, name: str, kind: str = "span", input: Any = None, **meta: Any
    ) -> Iterator[Observation]:
        ctx = current_ctx.get() or TraceCtx(uuid.uuid4().hex)
        obs = Observation(name, kind, ctx.trace_id, parent_id=ctx.parent_id, meta=dict(meta))
        self.observations.append(obs)
        token = current_ctx.set(TraceCtx(ctx.trace_id, obs.id))
        try:
            yield obs
        except Exception as e:
            if not _is_control_flow(e):
                obs.update(level="ERROR", error=str(e))
            raise
        finally:
            current_ctx.reset(token)

    def url(self, trace_id: str) -> str | None:
        return None

    def flush(self) -> None:
        return None


class LangfuseTracer(MemoryTracer):
    def __init__(self) -> None:
        super().__init__()
        from langfuse import Langfuse

        from jobfit.config import get_settings

        st = get_settings()
        self._lf = Langfuse(
            public_key=st.langfuse_public_key,
            secret_key=st.langfuse_secret_key,
            base_url=st.langfuse_base_url,
        )

    @contextmanager
    def run(self, name: str, input: Any, **meta: Any) -> Iterator[Observation]:
        trace_id = self._lf.create_trace_id()
        with self._lf.start_as_current_observation(
            trace_context={"trace_id": trace_id},
            name=name,
            as_type="chain",
            input=input,
            metadata=meta,
        ) as lf_obs:
            obs = Observation(name, "trace", trace_id, id=lf_obs.id, meta=dict(meta), _lf=lf_obs)
            self.observations.append(obs)
            token = current_ctx.set(TraceCtx(trace_id, lf_obs.id))
            try:
                yield obs
            finally:
                current_ctx.reset(token)

    @contextmanager
    def observe(
        self, name: str, kind: str = "span", input: Any = None, **meta: Any
    ) -> Iterator[Observation]:
        ctx = current_ctx.get()
        tc: dict[str, str] | None = None
        if ctx:
            tc = {"trace_id": ctx.trace_id}
            if ctx.parent_id:
                tc["parent_span_id"] = ctx.parent_id
        model = meta.pop("model", None)
        with self._lf.start_as_current_observation(  # type: ignore[call-overload]
            trace_context=tc,
            name=name,
            as_type=kind,
            input=input,
            metadata=meta or None,
            model=model,
        ) as lf_obs:
            obs = Observation(
                name,
                kind,
                ctx.trace_id if ctx else "",
                id=lf_obs.id,
                parent_id=ctx.parent_id if ctx else None,
                meta=dict(meta),
                _lf=lf_obs,
            )
            self.observations.append(obs)
            token = current_ctx.set(TraceCtx(obs.trace_id, lf_obs.id))
            try:
                yield obs
            except Exception as e:
                obs.update(level="ERROR", error=str(e))
                raise
            finally:
                current_ctx.reset(token)

    def url(self, trace_id: str) -> str | None:
        return self._lf.get_trace_url(trace_id=trace_id)

    def flush(self) -> None:
        self._lf.flush()


def get_tracer() -> MemoryTracer:
    from jobfit.config import get_settings

    st = get_settings()
    if st.langfuse_public_key and st.langfuse_secret_key:
        return LangfuseTracer()
    return MemoryTracer()
