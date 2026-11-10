"""Provider chain: Groq first, Claude when Groq can't answer. Offline (scripted providers)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from jobfit.llm import client as llm_client
from jobfit.llm.base import LLMError, QuotaExhausted
from jobfit.llm.client import LLM, ModelsConfig
from jobfit.llm.cost import InMemoryLedger
from jobfit.llm.fake import FakeProvider
from jobfit.schemas import RequirementOut, RequirementsOut

REQS = RequirementsOut(
    requirements=[RequirementOut(text="Python", kind="required", category="technical_skill")]
)


class Named(FakeProvider):
    def __init__(self, name: str, fail: BaseException | None = None) -> None:
        super().__init__()
        self.name = name
        self.fail = fail
        self.script(RequirementsOut, REQS)

    def structured(self, **kw: Any) -> Any:
        if self.fail is not None:
            self.calls.append(("fail", "", ""))
            raise self.fail
        return super().structured(**kw)


@pytest.fixture(autouse=True)
def _reset_bench() -> Iterator[None]:
    llm_client._benched_until.clear()
    yield
    llm_client._benched_until.clear()


def _llm(primary: Named, fallback: Named) -> LLM:
    return LLM(primary, ModelsConfig.load(), InMemoryLedger(), fallbacks=[fallback])


def test_quota_falls_back_and_benches_primary() -> None:
    groq = Named("groq", fail=QuotaExhausted("groq daily token limit reached"))
    claude = Named("anthropic")
    llm = _llm(groq, claude)
    llm.structured("extract_requirements", "s", "u", RequirementsOut)
    llm.structured("extract_requirements", "s", "u2", RequirementsOut)
    assert len(groq.calls) == 1  # benched after the first quota error, not retried per call
    assert len(claude.calls) == 2
    recs = [r for _, r in llm.ledger.records]  # type: ignore[attr-defined]
    assert {r.provider for r in recs} == {"anthropic"}
    assert recs[0].model == "fake:claude-haiku-5-5"  # Claude's own tier table was used


def test_one_off_failure_only_moves_that_call() -> None:
    groq = Named("groq", fail=LLMError("groq request failed: timeout"))
    claude = Named("anthropic")
    llm = _llm(groq, claude)
    llm.structured("extract_requirements", "s", "u", RequirementsOut)
    groq.fail = None
    llm.structured("extract_requirements", "s", "u2", RequirementsOut)
    assert [c[0] for c in groq.calls] == ["fail", "RequirementsOut"]  # tried again next call
    assert len(claude.calls) == 1


def test_everyone_out_of_quota_stops_cleanly() -> None:
    llm = _llm(
        Named("groq", fail=QuotaExhausted("groq out")),
        Named("anthropic", fail=QuotaExhausted("claude out")),
    )
    with pytest.raises(QuotaExhausted):
        llm.structured("extract_requirements", "s", "u", RequirementsOut)


def test_env_provider_pins_one_and_disables_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = ModelsConfig.load()
    assert cfg.provider == "groq" and cfg.fallbacks == ("anthropic",)
    assert cfg.eval_provider == "anthropic"
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    pinned = ModelsConfig.load()
    assert pinned.provider == "anthropic" and pinned.fallbacks == ()
