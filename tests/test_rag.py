"""Vector store behaviour. HashEmbedder offline; pgvector test needs DATABASE_URL."""

from __future__ import annotations

import os

import pytest

from jobfit.profile.chunks import build_chunks
from jobfit.profile.loader import load_profile
from jobfit.rag.embedder import HashEmbedder
from jobfit.rag.store import InMemoryStore, PgVectorStore


def test_inmemory_retrieves_relevant_evidence() -> None:
    store = InMemoryStore(HashEmbedder())
    chunks = build_chunks(load_profile())
    assert store.upsert(chunks) == len(chunks)
    assert store.upsert(chunks) == 0  # unchanged chunks are not re-embedded

    ids = [h.id for h in store.search("Python trading engine backtesting", k=3)]
    assert "exp.tycodes.4" in ids

    skills = store.search("TypeScript", k=3, kinds=["skill"])
    assert skills[0].id == "skill.typescript"
    assert all(h.kind == "skill" for h in skills)


@pytest.mark.db
@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs DATABASE_URL")
def test_pgvector_roundtrip() -> None:
    import psycopg

    dsn = os.environ["DATABASE_URL"]
    # WHY: own table so the test never overwrites the real index with hash vectors.
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("drop table if exists profile_chunks_test")
        conn.execute("create table profile_chunks_test (like profile_chunks including all)")
    try:
        store = PgVectorStore(dsn, HashEmbedder(), table="profile_chunks_test")
        chunks = build_chunks(load_profile())
        assert store.upsert(chunks) == len(chunks)
        assert store.upsert(chunks) == 0
        hits = store.search("Python trading engine backtesting", k=3)
        assert "exp.tycodes.4" in [h.id for h in hits]
        assert all(h.kind == "skill" for h in store.search("Python", k=3, kinds=["skill"]))
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("drop table if exists profile_chunks_test")


def test_search_evidence_returns_each_kind() -> None:
    from jobfit.rag.store import search_evidence

    store = InMemoryStore(HashEmbedder())
    store.upsert(build_chunks(load_profile()))
    hits = search_evidence(store, "Python data validation", {"bullet": 2, "skill": 2})
    assert {h.kind for h in hits} == {"bullet", "skill"}
    assert len(hits) == 4
