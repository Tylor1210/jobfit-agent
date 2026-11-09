"""Wire real dependencies for the graphs: profile, vector store, repo, LLM, renderer, checkpointer.

Used by the CLI now and by the API (Phase 5) and ingestion (Phase 3). Tests build Deps by hand
with fakes instead.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from jobfit.config import get_settings
from jobfit.graphs.common import Deps
from jobfit.graphs.tailor import load_voice_samples, make_file_renderer, make_page_counter
from jobfit.llm.client import build_llm
from jobfit.profile.chunks import build_chunks
from jobfit.profile.loader import load_constraints, load_profile
from jobfit.rag.embedder import get_embedder
from jobfit.rag.store import InMemoryStore, PgVectorStore, VectorStore
from jobfit.store.repo import InMemoryRepo, PgRepo, Repo


def build_deps(*, render: bool = True) -> Deps:
    s = get_settings()
    profile = load_profile()
    embedder = get_embedder()
    store: VectorStore
    repo: Repo
    if s.database_url:
        store, repo = PgVectorStore(s.database_url, embedder), PgRepo(s.database_url)
    else:
        # WHY: lets the CLI run with zero setup. Nothing persists between runs in this mode.
        store, repo = InMemoryStore(embedder), InMemoryRepo()
        store.upsert(build_chunks(profile))
    deps = Deps(
        llm=build_llm(),
        store=store,
        repo=repo,
        profile=profile,
        constraints=load_constraints(),
        voice_samples=load_voice_samples(),
    )
    if render:
        deps.page_count = make_page_counter(deps)
        deps.render = make_file_renderer(deps)
    return deps


def build_checkpointer() -> tuple[BaseCheckpointSaver[Any], Any]:
    """Return (checkpointer, closer). Postgres when DATABASE_URL is set so a paused review
    survives restarts; in-memory otherwise."""
    dsn = get_settings().database_url
    if not dsn:
        return InMemorySaver(), None
    from langgraph.checkpoint.postgres import PostgresSaver

    cm = PostgresSaver.from_conn_string(dsn)
    saver = cm.__enter__()
    saver.setup()  # idempotent: creates checkpoint tables on first use
    return saver, cm
