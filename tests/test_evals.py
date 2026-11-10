"""Eval harness: case loading, record/replay cache, gap matching, and the CI gate. Offline."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobfit.evals.cache import CacheMiss, CachingProvider
from jobfit.evals.cases import ExpectedGap, load_cases
from jobfit.evals.runner import CaseResult, _within_one, summarize
from jobfit.llm.base import TierConfig
from jobfit.llm.fake import FakeProvider
from jobfit.schemas import RequirementOut, RequirementsOut

TIER = TierConfig(model="m", input_per_mtok=1.0, output_per_mtok=1.0)
REQS = RequirementsOut(
    requirements=[RequirementOut(text="Python", kind="required", category="technical_skill")]
)


def test_cases_file_is_valid() -> None:
    cases = load_cases()
    assert len(cases) >= 10
    ready = [c for c in cases if c.runnable()]
    assert {c.id for c in ready} >= {
        "tribalscale-fde",
        "smartsheet-sa",
        "tebra-staff-ai",
        "injection-obvious",
    }
    assert any(c.injection == "obvious" for c in cases)
    assert all(c.max_score is not None for c in cases if c.injection)


def test_cache_records_then_replays(tmp_path: Path) -> None:
    fake = FakeProvider().script(RequirementsOut, REQS)
    rec = CachingProvider(fake, "groq", "auto", tmp_path)
    out1, u1, _ = rec.structured(tier=TIER, system="s", user="u", schema=RequirementsOut)
    assert len(fake.calls) == 1 and not rec.events[0].hit

    replay = CachingProvider(None, "groq", "replay", tmp_path)
    out2, u2, _ = replay.structured(tier=TIER, system="s", user="u", schema=RequirementsOut)
    assert out2 == out1 and u2 == u1 and replay.events[0].hit
    with pytest.raises(CacheMiss):
        replay.structured(tier=TIER, system="s", user="changed prompt", schema=RequirementsOut)


def test_gap_matching_and_bands() -> None:
    g = ExpectedGap(name="Kafka", match=["kafka"])
    assert g.found_in("Missing: Apache Kafka streaming") and not g.found_in("Python")
    assert _within_one("strong", "moderate") and not _within_one("strong", "weak")
    assert not _within_one("unknown", "weak")


def test_gate_fails_on_hallucination_injection_or_error() -> None:
    ok = CaseResult(
        "a",
        "ok",
        "strong",
        verdict="strong",
        verdict_ok=True,
        within_one=True,
        tailored=True,
        first_draft_hallucinations=3,
        final_hallucinations=0,
    )
    s = summarize([ok], "groq", "replay", True)
    assert s.gate_passed and s.first_draft_hallucinations == 3

    bad = CaseResult("b", "ok", "weak", verdict="weak", injection="obvious", injection_ok=False)
    halluc = CaseResult("c", "ok", "strong", tailored=True, final_hallucinations=1)
    err = CaseResult("d", "error", "weak", error="boom")
    s = summarize([ok, bad, halluc, err], "groq", "replay", True)
    assert not s.gate_passed and len(s.gate_failures) == 3

    skipped = CaseResult("e", "skipped", "weak")
    assert summarize([ok, skipped], "groq", "replay", True).cases_skipped == ["e"]
