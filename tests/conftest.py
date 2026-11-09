"""Shared fixtures: offline Deps (fake LLM, hash embeddings, in-memory repo and tracer)."""

from __future__ import annotations

import pytest

from jobfit.graphs.common import Deps
from jobfit.llm.client import LLM, ModelsConfig
from jobfit.llm.cost import InMemoryLedger
from jobfit.llm.fake import FakeProvider
from jobfit.profile.chunks import build_chunks
from jobfit.profile.loader import load_constraints, load_profile
from jobfit.rag.embedder import HashEmbedder
from jobfit.rag.store import InMemoryStore
from jobfit.schemas import JobPosting
from jobfit.store.repo import InMemoryRepo
from jobfit.tracing import MemoryTracer

POSTING = """Forward Deployed Engineer, TribalScale (Fort Lauderdale, hybrid)
You will build AI agent workflows with tool calling for enterprise clients.
Requirements: 3+ years building production software. Python and TypeScript.
Experience with LLM APIs (Anthropic, OpenAI). Client-facing communication.
Nice to have: Kubernetes, Terraform."""


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def deps(fake: FakeProvider) -> Deps:
    profile = load_profile()
    store = InMemoryStore(HashEmbedder())
    store.upsert(build_chunks(profile))
    llm = LLM(fake, ModelsConfig.load(), InMemoryLedger(), MemoryTracer())
    return Deps(
        llm=llm,
        store=store,
        repo=InMemoryRepo(),
        profile=profile,
        constraints=load_constraints(),
        verify_with_llm=False,
    )


@pytest.fixture
def job() -> JobPosting:
    return JobPosting(
        id="greenhouse:1",
        source="greenhouse",
        external_id="1",
        company="TribalScale",
        title="Forward Deployed Engineer",
        description=POSTING,
    )
