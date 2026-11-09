"""Embedding backends behind one small interface.

FastEmbedder is the real one (local ONNX, free). HashEmbedder is deterministic and offline, used
by unit tests and CI so they never download a model.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

Vector = NDArray[np.float32]
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[Vector]: ...
    def embed_query(self, text: str) -> Vector: ...


class FastEmbedder:
    def __init__(self, model_name: str, dim: int) -> None:
        from fastembed import TextEmbedding  # lazy: import is slow and pulls onnxruntime

        # Cache dir comes from FASTEMBED_CACHE_PATH when set (CI caches it between runs).
        self._model = TextEmbedding(model_name=model_name)
        self.name = model_name
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        return [np.asarray(v, dtype=np.float32) for v in self._model.embed(list(texts))]

    def embed_query(self, text: str) -> Vector:
        # WHY: bge-en models are asymmetric and expect this prefix on queries only.
        # GOTCHA: fastembed's query_embed() does not add it for bge-small, so we add it ourselves.
        (v,) = self.embed([BGE_QUERY_PREFIX + text])
        return v


class HashEmbedder:
    """Bag-of-words hashed into a fixed vector. Crude, but similar texts land close together."""

    def __init__(self, dim: int = 384) -> None:
        self.name = f"hash-{dim}"
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        out: list[Vector] = []
        for t in texts:
            v = np.zeros(self.dim, dtype=np.float32)
            for tok in re.findall(r"[a-z0-9+#.]+", t.lower()):
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                v[h % self.dim] += 1.0
            n = np.linalg.norm(v)
            out.append(v / n if n else v)
        return out

    def embed_query(self, text: str) -> Vector:
        return self.embed([text])[0]


def get_embedder() -> Embedder:
    from jobfit.config import get_settings

    s = get_settings()
    return FastEmbedder(s.embedding_model, s.embedding_dim)
