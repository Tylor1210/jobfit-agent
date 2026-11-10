"""Record/replay cache for LLM calls, so evals are reproducible, free to re-run, and work in CI.

Wraps any Provider. Each call is keyed by (model, output schema, system prompt, user prompt).
- auto:   replay on hit, call the real provider and record on miss (default)
- replay: never call the provider; a miss is an error (CI without keys)
- record: always call the provider and overwrite (refresh after prompt or model changes)

Recorded latency and token usage are replayed too, so a replayed report shows what the live run
measured instead of ~0 ms.

GOTCHA: any prompt change is a cache miss by design. After changing a prompt, run
`make eval-record` once with a key, then commit evals/cache/.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from jobfit.config import ROOT
from jobfit.llm.base import Provider, T, TierConfig, Usage

CACHE_DIR = ROOT / "evals" / "cache"
Mode = Literal["auto", "replay", "record"]


class CacheMiss(RuntimeError):
    pass


@dataclass
class CallEvent:
    schema: str
    hit: bool
    latency_ms: int
    usage: Usage
    missed: bool = False  # no recording and no provider to call


@dataclass
class CachingProvider:
    inner: Provider | None
    name: str  # the provider whose tier table/prices apply (config provider)
    mode: Mode = "auto"
    cache_dir: Path = CACHE_DIR
    events: list[CallEvent] = field(default_factory=list)

    @staticmethod
    def key(tier: TierConfig, system: str, user: str, schema: type[Any]) -> str:
        blob = json.dumps([tier.model, schema.__name__, system, user], ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def structured(
        self, *, tier: TierConfig, system: str, user: str, schema: type[T]
    ) -> tuple[T, Usage, str]:
        k = self.key(tier, system, user, schema)
        path = self._path(k)
        if self.mode != "record" and path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            usage = Usage(**d["usage"])
            self.events.append(CallEvent(schema.__name__, True, d["latency_ms"], usage))
            return schema.model_validate(d["output"]), usage, d["model"]
        if self.mode == "replay" or self.inner is None:
            self.events.append(CallEvent(schema.__name__, False, 0, Usage(), missed=True))
            raise CacheMiss(
                f"no recording for {schema.__name__} ({k[:12]}); "
                "run `make eval-record` with an API key"
            )
        t0 = time.perf_counter()
        out, usage, model = self.inner.structured(
            tier=tier, system=system, user=user, schema=schema
        )
        latency = int((time.perf_counter() - t0) * 1000)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": schema.__name__,
                    "model": model,
                    "latency_ms": latency,
                    "usage": _usage_dict(usage),
                    "prompt_head": user[:160],
                    "output": out.model_dump(mode="json"),
                },
                indent=1,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.events.append(CallEvent(schema.__name__, False, latency, usage))
        return out, usage, model


def _usage_dict(u: Usage) -> dict[str, int]:
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": u.cache_read_tokens,
        "cache_write_tokens": u.cache_write_tokens,
    }


EMBED_CACHE = CACHE_DIR / "embeddings.npz"


class CachedEmbedder:
    """Record/replay for embedding vectors, keyed by (model, query?, text).

    WHY: ONNX gives slightly different floats on x86 (CI) than on Apple Silicon. When two
    evidence chunks score nearly the same, their order flips, the prompt text changes, and every
    downstream LLM recording misses. Replaying the recorded vectors makes CI retrieval identical.
    Misses fall through to the real embedder (they don't fail the run on their own).
    """

    def __init__(self, inner: Any, path: Path = EMBED_CACHE) -> None:
        import numpy as np

        self._np = np
        self.inner = inner
        self.name = inner.name
        self.dim = inner.dim
        self.path = path
        self._vecs: dict[str, Any] = {}
        self.dirty = False
        if path.exists():
            with np.load(path) as z:
                self._vecs = {k: z[k] for k in z.files}

    def _key(self, text: str, query: bool) -> str:
        raw = f"{self.name}|{'q' if query else 'p'}|{text}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def embed(self, texts: Any) -> list[Any]:
        texts = list(texts)
        keys = [self._key(t, False) for t in texts]
        todo = [i for i, k in enumerate(keys) if k not in self._vecs]
        if todo:
            for i, v in zip(todo, self.inner.embed([texts[i] for i in todo]), strict=True):
                self._vecs[keys[i]] = v
            self.dirty = True
        return [self._vecs[k] for k in keys]

    def embed_query(self, text: str) -> Any:
        k = self._key(text, True)
        if k not in self._vecs:
            self._vecs[k] = self.inner.embed_query(text)
            self.dirty = True
        return self._vecs[k]

    def save(self) -> None:
        if self.dirty:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._np.savez_compressed(self.path, **self._vecs)
            self.dirty = False
