"""Vector store for profile evidence. pgvector in Supabase is the real store; an in-memory store
with the same interface backs tests and offline evals.

Used by the indexer (upsert) and the `retrieve_evidence` node (search).
Depends on db/migrations/001_profile_chunks.sql for the Postgres table.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

from jobfit.profile.chunks import Chunk
from jobfit.rag.embedder import Embedder, Vector

if TYPE_CHECKING:
    import psycopg


@dataclass(frozen=True, slots=True)
class Hit:
    id: str
    kind: str
    text: str
    score: float  # cosine similarity, 1.0 is identical


class VectorStore(Protocol):
    def upsert(self, chunks: Sequence[Chunk]) -> int: ...
    def search(self, query: str, k: int = 5, kinds: Sequence[str] | None = None) -> list[Hit]: ...


def search_evidence(
    store: VectorStore, query: str, per_kind: dict[str, int] | None = None
) -> list[Hit]:
    """Top hits per chunk kind, merged by score.

    WHY: short skill chunks ("Skill: Python") score higher than long bullets for almost any
    query, so a single top-k returns only skills. Searching each kind separately guarantees the
    scorer sees both the skill claim and the experience that backs it.
    """
    per_kind = per_kind or {"bullet": 3, "skill": 2, "education": 1, "certification": 1}
    hits = [h for kind, k in per_kind.items() for h in store.search(query, k=k, kinds=[kind])]
    return sorted(hits, key=lambda h: h.score, reverse=True)


class InMemoryStore:
    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        self._rows: dict[str, tuple[Chunk, Vector]] = {}

    def upsert(self, chunks: Sequence[Chunk]) -> int:
        m = self._embedder.name
        todo = [
            c
            for c in chunks
            if c.id not in self._rows or self._rows[c.id][0].content_hash(m) != c.content_hash(m)
        ]
        for c, v in zip(todo, self._embedder.embed([c.text for c in todo]), strict=True):
            self._rows[c.id] = (c, v)
        return len(todo)

    def search(self, query: str, k: int = 5, kinds: Sequence[str] | None = None) -> list[Hit]:
        q = self._embedder.embed_query(query)
        scored = [
            Hit(c.id, c.kind, c.text, float(np.dot(q, v)))
            for c, v in self._rows.values()
            if kinds is None or c.kind in kinds
        ]
        return sorted(scored, key=lambda h: h.score, reverse=True)[:k]


class PgVectorStore:
    def __init__(self, dsn: str, embedder: Embedder, table: str = "profile_chunks") -> None:
        # CONSTRAINT: `table` is interpolated into SQL, so it must be a plain identifier.
        if not table.isidentifier():
            raise ValueError(f"bad table name: {table!r}")
        self._dsn = dsn
        self._embedder = embedder
        self._table = table

    def _connect(self) -> psycopg.Connection[Any]:
        import psycopg
        from pgvector.psycopg import register_vector

        conn = psycopg.connect(self._dsn, autocommit=True)
        register_vector(conn)
        return conn

    def upsert(self, chunks: Sequence[Chunk]) -> int:
        with self._connect() as conn:
            m = self._embedder.name
            t = self._table
            existing = dict(conn.execute(f"select id, content_hash from {t}").fetchall())
            todo = [c for c in chunks if existing.get(c.id) != c.content_hash(m)]
            vecs = self._embedder.embed([c.text for c in todo]) if todo else []
            with conn.cursor() as cur:
                for c, v in zip(todo, vecs, strict=True):
                    cur.execute(
                        f"""insert into {t}
                             (id, kind, text, meta, content_hash, embedding)
                           values (%s, %s, %s, %s, %s, %s)
                           on conflict (id) do update set kind = excluded.kind,
                             text = excluded.text, meta = excluded.meta,
                             content_hash = excluded.content_hash,
                             embedding = excluded.embedding, updated_at = now()""",
                        (c.id, c.kind, c.text, json.dumps(c.meta), c.content_hash(m), v),
                    )
                # Drop chunks whose evidence ID no longer exists in the profile.
                cur.execute(f"delete from {t} where not (id = any(%s))", ([c.id for c in chunks],))
            return len(todo)

    def search(self, query: str, k: int = 5, kinds: Sequence[str] | None = None) -> list[Hit]:
        q = self._embedder.embed_query(query)
        with self._connect() as conn:
            rows = conn.execute(
                f"""select id, kind, text, 1 - (embedding <=> %s) as score
                   from {self._table}
                   where %s::text[] is null or kind = any(%s::text[])
                   order by embedding <=> %s limit %s""",
                (q, list(kinds) if kinds else None, list(kinds) if kinds else None, q, k),
            ).fetchall()
        return [Hit(r[0], r[1], r[2], float(r[3])) for r in rows]
