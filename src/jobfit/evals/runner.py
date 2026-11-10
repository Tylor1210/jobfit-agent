"""Eval runner: runs every ready case through the real fit graph (and tailor graph where flagged)
and measures verdict accuracy, gap recall, hallucinated claims, injection resistance, latency,
tokens, and cost. Called by `jobfit eval` / `make eval`.

CI gates (exit code 1): any hallucinated claim in final drafts, any failed injection case, or any
case that errored. Verdict accuracy and gap recall are reported, not gated: they measure judgment,
and a single disagreement should prompt a look, not block a deploy.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from jobfit.evals.cache import CacheMiss, CachingProvider, Mode
from jobfit.evals.cases import Case
from jobfit.graphs.common import Deps
from jobfit.graphs.fit import run_fit
from jobfit.graphs.tailor import load_voice_samples, start_tailor
from jobfit.graphs.verify import hallucination_count
from jobfit.llm.client import LLM, ModelsConfig, build_provider
from jobfit.llm.cost import InMemoryLedger
from jobfit.profile.chunks import build_chunks
from jobfit.profile.loader import load_constraints, load_profile
from jobfit.rag.embedder import Embedder
from jobfit.rag.store import InMemoryStore
from jobfit.schemas import Issue
from jobfit.store.repo import InMemoryRepo
from jobfit.tracing import MemoryTracer

BANDS = ["not_a_fit", "weak", "moderate", "strong"]


@dataclass
class CaseResult:
    id: str
    status: str  # ok | error | skipped
    expected_verdict: str
    verdict: str | None = None
    fit_score: int | None = None
    verdict_ok: bool | None = None
    within_one: bool | None = None
    gaps_expected: int = 0
    gaps_found: int = 0
    gaps_missed: list[str] = field(default_factory=list)
    predicted_gaps: list[str] = field(default_factory=list)
    injection: str | None = None
    injection_flagged: bool | None = None
    injection_ok: bool | None = None
    tailored: bool = False
    first_draft_hallucinations: int | None = None
    final_hallucinations: int | None = None
    verify_rounds: int | None = None
    final_issue_rules: list[str] = field(default_factory=list)
    llm_calls: int = 0
    cache_hits: int = 0
    cache_misses: int = 0  # needed a recording that doesn't exist (prompt or profile changed)
    latency_ms: int = 0  # recorded LLM latency + local compute
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None


def _within_one(expected: str, got: str) -> bool:
    if expected == got:
        return True
    if expected not in BANDS or got not in BANDS:
        return False
    return abs(BANDS.index(expected) - BANDS.index(got)) <= 1


def _page_counter(deps: Deps) -> Any:
    try:
        from jobfit.graphs.tailor import make_page_counter
        from jobfit.render.pdf import default_renderer

        default_renderer()  # fail fast if Chromium is not installed
        return make_page_counter(deps)
    except Exception:
        return None  # page check skipped; reported in the summary


def run_case(case: Case, base: Deps, provider: CachingProvider) -> CaseResult:
    res = CaseResult(case.id, "ok", case.expected_verdict, injection=case.injection)
    # Fresh repo/ledger/tracer per case so numbers are attributable; the vector store is shared.
    ledger = InMemoryLedger()
    llm = LLM(provider, base.llm.config, ledger, MemoryTracer(), run_id=f"eval:{case.id}")
    deps = Deps(
        llm=llm,
        store=base.store,
        repo=InMemoryRepo(),
        profile=base.profile,
        constraints=base.constraints,
        page_count=base.page_count,
        verify_with_llm=True,
        voice_samples=base.voice_samples,
    )
    n_events = len(provider.events)
    t0 = time.perf_counter()
    try:
        out = run_fit(case.job(), deps)
        if out.get("error"):
            raise RuntimeError(out["error"])
        fit = out["fit"]
        res.verdict, res.fit_score = fit.verdict, fit.fit_score
        res.verdict_ok = fit.verdict == case.expected_verdict
        res.within_one = _within_one(case.expected_verdict, fit.verdict)
        res.predicted_gaps = fit.top_gaps
        gap_text = " | ".join(
            [*fit.top_gaps, *(a.requirement.text for a in fit.assessments if a.status != "met")]
        )
        res.gaps_expected = len(case.expected_gaps)
        res.gaps_missed = [g.name for g in case.expected_gaps if not g.found_in(gap_text)]
        res.gaps_found = res.gaps_expected - len(res.gaps_missed)

        if case.injection:
            res.injection_flagged = bool(fit.injection_flags)
            not_inflated = case.max_score is None or fit.fit_score <= case.max_score
            # The obvious attack must be caught by the guard. The subtle one only has to fail
            # to move the score (defense in depth: the score is computed in code).
            res.injection_ok = not_inflated and (
                res.injection_flagged if case.injection == "obvious" else True
            )

        if case.tailor:
            t = start_tailor(case.job().id, deps, InMemorySaver())
            if t.get("error"):
                raise RuntimeError(f"tailor: {t['error']}")
            log = t.get("verify_log", [])
            res.tailored = True
            res.verify_rounds = len(log)
            res.first_draft_hallucinations = log[0]["hallucinations"] if log else None
            final = t["__interrupt__"][0].value["issues"] if t.get("__interrupt__") else []
            issues = [Issue.model_validate(i) for i in final]
            res.final_hallucinations = hallucination_count(issues)
            res.final_issue_rules = sorted({i.rule for i in issues})
    except CacheMiss as e:
        res.status, res.error = "error", str(e)
    except Exception as e:
        res.status, res.error = "error", f"{type(e).__name__}: {e}"[:400]
    finally:
        wall = int((time.perf_counter() - t0) * 1000)
        events = provider.events[n_events:]
        res.llm_calls = len(events)
        res.cache_hits = sum(e.hit for e in events)
        res.cache_misses = sum(e.missed for e in events)
        # Wall time already includes live calls; add back the latency that replays skipped.
        res.latency_ms = wall + sum(e.latency_ms for e in events if e.hit)
        res.input_tokens = sum(r.input_tokens for _, r in ledger.records)
        res.output_tokens = sum(r.output_tokens for _, r in ledger.records)
        res.cost_usd = round(sum(r.cost_usd for _, r in ledger.records), 6)
    return res


@dataclass
class Summary:
    run_at: str
    provider: str
    mode: str
    cases_run: int
    cases_skipped: list[str]
    cases_errored: list[str]
    verdict_accuracy: float | None
    verdict_within_one: float | None
    gap_recall: float | None
    hallucinated_claims: int  # in final drafts (gated: must be 0)
    first_draft_hallucinations: int  # before the verify loop (informational)
    injection_passed: int
    injection_total: int
    total_latency_ms: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    cache_hit_rate: float | None
    cache_misses: int
    page_check: bool
    gate_passed: bool
    gate_failures: list[str]


def summarize(results: list[CaseResult], provider: str, mode: str, page_check: bool) -> Summary:
    ran = [r for r in results if r.status != "skipped"]
    ok = [r for r in ran if r.status == "ok"]

    def ratio(n: int, d: int) -> float | None:
        return round(n / d, 3) if d else None

    gaps_exp = sum(r.gaps_expected for r in ok)
    inj = [r for r in ok if r.injection]
    halluc = sum(r.final_hallucinations or 0 for r in ok)
    calls = sum(r.llm_calls for r in ran)
    failures: list[str] = []
    if halluc:
        failures.append(f"{halluc} hallucinated claim(s) in final drafts")
    failures += [f"injection case {r.id} failed" for r in inj if not r.injection_ok]
    misses = sum(r.cache_misses for r in ran)
    if misses:
        failures.append(
            f"{misses} LLM call(s) had no recording (prompts, profile or retrieval changed). "
            "Run `make eval-record` with GROQ_API_KEY set and commit evals/cache/."
        )
    failures += [
        f"case {r.id} errored: {r.error}" for r in ran if r.status == "error" and not r.cache_misses
    ]
    return Summary(
        run_at=datetime.now(UTC).isoformat(timespec="seconds"),
        provider=provider,
        mode=mode,
        cases_run=len(ran),
        cases_skipped=[r.id for r in results if r.status == "skipped"],
        cases_errored=[r.id for r in ran if r.status == "error"],
        verdict_accuracy=ratio(sum(bool(r.verdict_ok) for r in ok), len(ok)),
        verdict_within_one=ratio(sum(bool(r.within_one) for r in ok), len(ok)),
        gap_recall=ratio(sum(r.gaps_found for r in ok), gaps_exp),
        hallucinated_claims=halluc,
        first_draft_hallucinations=sum(r.first_draft_hallucinations or 0 for r in ok),
        injection_passed=sum(bool(r.injection_ok) for r in inj),
        injection_total=len([c for c in ran if c.injection]),
        total_latency_ms=sum(r.latency_ms for r in ran),
        total_input_tokens=sum(r.input_tokens for r in ran),
        total_output_tokens=sum(r.output_tokens for r in ran),
        total_cost_usd=round(sum(r.cost_usd for r in ran), 6),
        cache_hit_rate=ratio(sum(r.cache_hits for r in ran), calls),
        cache_misses=sum(r.cache_misses for r in ran),
        page_check=page_check,
        gate_passed=not failures,
        gate_failures=failures,
    )


def _has_key(provider: str) -> bool:
    import os

    from jobfit.config import get_settings

    st = get_settings()
    if provider == "groq":
        return bool(st.groq_api_key)
    if provider == "anthropic":
        return bool(st.anthropic_api_key or os.getenv("ANTHROPIC_AUTH_TOKEN"))
    return False


def run_eval(
    cases: list[Case], embedder: Embedder, mode: Mode = "auto", tailor: bool = True
) -> tuple[list[CaseResult], Summary]:
    cfg = ModelsConfig.load()
    inner = None
    # WHY: without a key the provider would fail at call time with a vague network error
    # (CI turned a missing secret into "Connection error"). No key = replay only, and a miss
    # says plainly that recordings need refreshing.
    if mode != "replay" and _has_key(cfg.provider):
        inner = build_provider(cfg.provider)
    provider = CachingProvider(inner, cfg.provider, mode)
    profile = load_profile()
    store = InMemoryStore(embedder)
    store.upsert(build_chunks(profile))
    base = Deps(
        llm=LLM(provider, cfg, InMemoryLedger(), MemoryTracer()),
        store=store,
        repo=InMemoryRepo(),
        profile=profile,
        constraints=load_constraints(),
        voice_samples=load_voice_samples(),
    )
    base.page_count = _page_counter(base) if tailor else None

    results: list[CaseResult] = []
    for case in cases:
        if not case.runnable():
            results.append(
                CaseResult(case.id, "skipped", case.expected_verdict, error="needs posting text")
            )
            continue
        c = case if tailor else case.model_copy(update={"tailor": False})
        results.append(run_case(c, base, provider))
    return results, summarize(results, cfg.provider, mode, base.page_count is not None)


def to_json(results: list[CaseResult], summary: Summary) -> dict[str, Any]:
    return {"summary": asdict(summary), "cases": [asdict(r) for r in results]}
