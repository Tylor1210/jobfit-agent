"""Fit graph: sanitize -> extract_requirements -> retrieve_evidence -> score_fit -> save.

Runs on each candidate job (daily ingestion top-N, manual "Add job", evals).
Entry point: run_fit(job, deps). One trace per run.
"""

from __future__ import annotations

import itertools
import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from jobfit.graphs import prompts
from jobfit.graphs.common import Deps, State, node, stop_on_error
from jobfit.graphs.sanitize import sanitize, wrap
from jobfit.profile.models import Profile
from jobfit.rag.store import search_evidence
from jobfit.schemas import (
    Assessment,
    EvidenceRef,
    FitOut,
    FitResult,
    JobPosting,
    Requirement,
    RequirementsOut,
    Status,
    Verdict,
)

MAX_REQUIREMENTS = 15
MIN_REQUIREMENTS = 2
EVIDENCE_PER_REQ = 5
_PER_KIND = {
    "bullet": 3,
    "skill": 2,
    "summary": 1,
    "education": 1,
    "certification": 1,
    "contact": 1,
}

# WHY: the score is computed in code, not by the model, so a posting cannot talk its way to a
# high number (injection defense) and scores are consistent across providers.
_WEIGHT = {"required": 3.0, "preferred": 1.0}
_CREDIT = {"met": 1.0, "partial": 0.5, "missing": 0.0}
# A missing *required* item in these categories caps the score: no amount of tool overlap makes
# up for 8+ years asked vs ~4 held, or a clearance Tylor does not have.
_KNOCKOUT = {"years_experience", "clearance", "education"}
KNOCKOUT_CAP = 54
# Asked for far more years than Tylor has (less than 60% of the ask, e.g. 4 vs 8+): not a fit,
# however well the tools line up. Also enforced in code so the model can't call it "partial".
SEVERE_YEARS_RATIO = 0.6
SEVERE_YEARS_CAP = 34
_YEARS = re.compile(r"(\d+)\s*\+?\s*(?:(?:-|to|\u2013)\s*(\d+)\s*)?\+?\s*years?", re.I)


def required_years(text: str) -> int | None:
    """Lowest number of years a requirement asks for: "8+ years" -> 8, "1-3 years" -> 1."""
    nums = [int(m.group(1)) for m in _YEARS.finditer(text)]
    return min(nums) if nums else None


def judge_years(text: str, have: int) -> Status | None:
    need = required_years(text)
    if need is None:
        return None
    if have >= need:
        return "met"
    return "partial" if have >= need * 0.75 else "missing"


class FitState(TypedDict, total=False):
    job: JobPosting
    trace_id: str
    trace_root: str | None
    clean_text: str
    flags: list[str]
    requirements: list[Requirement]
    evidence: dict[str, list[EvidenceRef]]
    fit: FitResult
    error: str
    error_node: str


def verdict_for(score: int) -> Verdict:
    if score >= 75:
        return "strong"
    if score >= 55:
        return "moderate"
    if score >= 35:
        return "weak"
    return "not_a_fit"


def compute_score(assessments: list[Assessment], years_have: int | None = None) -> int:
    if not assessments:
        return 0
    total = sum(_WEIGHT[a.requirement.kind] for a in assessments)
    got = sum(_WEIGHT[a.requirement.kind] * _CREDIT[a.status] for a in assessments)
    score = round(100 * got / total)
    knocked = any(
        a.requirement.kind == "required"
        and a.status == "missing"
        and a.requirement.category in _KNOCKOUT
        for a in assessments
    )
    if years_have is not None:
        for a in assessments:
            need = required_years(a.requirement.text)
            if (
                a.requirement.kind == "required"
                and a.requirement.category == "years_experience"
                and need
                and years_have < need * SEVERE_YEARS_RATIO
            ):
                return min(score, SEVERE_YEARS_CAP)
    return min(score, KNOCKOUT_CAP) if knocked else score


def exact_skill_index(profile: Profile) -> list[tuple[str, str, re.Pattern[str]]]:
    """(skill id, chunk-style text, matcher) for every skill name and alias, longest first."""
    out: list[tuple[str, str, re.Pattern[str]]] = []
    for cat in profile.skills:
        for sk in cat.items:
            for name in [sk.name.split(" (")[0], *sk.aliases]:
                # Also match the run-together form ("NodeJS" for "Node.js").
                variants = {name, name.replace(".", "").replace(" ", "")}
                for v in variants:
                    if len(v) < 2:
                        continue
                    rx = re.compile(rf"(?<![\w.]){re.escape(v)}(?![\w])", re.I)
                    out.append((sk.id, f"Skill in {cat.category}: {sk.name}", rx))
    return sorted(out, key=lambda t: -len(t[2].pattern))


def exact_skill_hits(
    text: str, index: list[tuple[str, str, re.Pattern[str]]]
) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for sid, chunk_text, rx in index:
        if sid not in seen and rx.search(text):
            seen[sid] = chunk_text
    return list(seen.items())


def build_fit_graph(deps: Deps) -> CompiledStateGraph[Any, Any, Any, Any]:
    @node("sanitize", deps)
    def sanitize_node(state: State) -> State:
        job: JobPosting = state["job"]
        s = sanitize(job.description)
        return {"clean_text": s.text, "flags": s.flags}

    @node("extract_requirements", deps)
    def extract(state: State) -> State:
        job: JobPosting = state["job"]
        user = f"Role: {job.title}\nCompany: {job.company}\n\n{wrap(state['clean_text'])}"
        res = deps.llm.structured(
            "extract_requirements", prompts.EXTRACT_REQUIREMENTS, user, RequirementsOut
        )
        seen: set[str] = set()
        reqs: list[Requirement] = []
        for r in res.output.requirements:
            key = r.text.strip().lower()
            if key and key not in seen and len(reqs) < MAX_REQUIREMENTS:
                seen.add(key)
                reqs.append(Requirement(id=f"r{len(reqs) + 1}", **r.model_dump()))
        return {"requirements": reqs}

    skill_names = exact_skill_index(deps.profile)

    @node("retrieve_evidence", deps)
    def retrieve(state: State) -> State:
        ev: dict[str, list[EvidenceRef]] = {}
        for r in state["requirements"]:
            hits = search_evidence(deps.store, r.text, _PER_KIND)[:EVIDENCE_PER_REQ]
            refs = [EvidenceRef(id=h.id, text=h.text, score=round(h.score, 3)) for h in hits]
            # Hybrid retrieval: a skill named verbatim in the requirement is always evidence.
            # WHY: vector search alone missed Node.js for "two or more of .Net, NodeJS, Python,
            # Java, Ruby" (one embedding per requirement blurs a list of five languages).
            have = {e.id for e in refs}
            for sid, text in exact_skill_hits(r.text, skill_names):
                if sid not in have:
                    refs.append(EvidenceRef(id=sid, text=text, score=1.0))
                    have.add(sid)
            ev[r.id] = refs
        return {"evidence": ev}

    @node("score_fit", deps)
    def score(state: State) -> State:
        job: JobPosting = state["job"]
        reqs: list[Requirement] = state["requirements"]
        ev: dict[str, list[EvidenceRef]] = state["evidence"]
        if len(reqs) < MIN_REQUIREMENTS:
            # WHY: no requirements means "can't tell", not "bad fit". Skip the paid call and
            # mark it unknown so the dashboard can say "open the posting" instead of scoring 0.
            return {
                "fit": FitResult(
                    fit_score=0,
                    verdict="unknown",
                    verdict_line="Too little posting text to assess; open the original listing.",
                    assessments=[],
                    top_gaps=[],
                    injection_flags=state.get("flags", []),
                )
            }
        # Token budget: the model sees requirements + retrieved evidence, not the posting again.
        lines = [f"Role: {job.title} at {job.company}", "<requirements>"]
        for r in reqs:
            lines.append(f"[{r.id}] ({r.kind}, {r.category}) {r.text}")
            lines += [f"    - {e.id}: {e.text[:220]}" for e in ev.get(r.id, [])]
        lines.append("</requirements>")
        res = deps.llm.structured("score_fit", prompts.SCORE_FIT, "\n".join(lines), FitOut)

        by_req = {a.req_id: a for a in res.output.assessments}
        assessments: list[Assessment] = []
        for r in reqs:
            a = by_req.get(r.id)
            allowed = {e.id: e for e in ev.get(r.id, [])}
            if a is None:
                assessments.append(
                    Assessment(requirement=r, status="missing", evidence=[], note="not assessed")
                )
                continue
            # Anti-hallucination: keep only IDs that were actually retrieved for this requirement.
            cited = [allowed[i] for i in dict.fromkeys(a.evidence_ids) if i in allowed]
            status = a.status
            note = a.note
            if r.category == "years_experience":
                yrs = judge_years(r.text, deps.profile.years_experience)
                if yrs and yrs != status:
                    note = f"{deps.profile.years_experience} years vs asked: {r.text}"
                    status = yrs
                    if status != "missing" and not cited:
                        summary = next((e for e in ev.get(r.id, []) if e.id == "sum.1"), None)
                        cited = [
                            summary
                            or EvidenceRef(id="sum.1", text=deps.profile.summary[0].text, score=1.0)
                        ]
            if status != "missing" and not cited:
                status, note = "missing", f"no valid evidence cited ({note})"
            assessments.append(Assessment(requirement=r, status=status, evidence=cited, note=note))

        fit_score = compute_score(assessments, deps.profile.years_experience)
        # GOTCHA: models sometimes return requirement IDs ("r7") as gaps; map them back to text.
        req_text = {r.id: r.text for r in reqs}
        gaps = [req_text.get(g.strip(), g.strip()) for g in res.output.top_gaps if g.strip()]
        gaps = (
            list(dict.fromkeys(gaps))[:3]
            or [
                a.requirement.text
                for a in assessments
                if a.status == "missing" and a.requirement.kind == "required"
            ][:3]
        )
        fit = FitResult(
            fit_score=fit_score,
            verdict=verdict_for(fit_score),
            verdict_line=res.output.verdict_line.strip(),
            assessments=assessments,
            top_gaps=gaps,
            injection_flags=state.get("flags", []),
        )
        return {"fit": fit}

    @node("save", deps)
    def save(state: State) -> State:
        job: JobPosting = state["job"]
        fit: FitResult = state["fit"]
        url = deps.llm.tracer.url(state["trace_id"]) if state.get("trace_id") else None
        if url:
            fit = fit.model_copy(update={"trace_url": url})
        deps.repo.upsert_job(job)
        deps.repo.save_fit(job.id, fit)
        return {"fit": fit}

    g: Any = StateGraph(FitState)
    for name, fn in [
        ("sanitize", sanitize_node),
        ("extract_requirements", extract),
        ("retrieve_evidence", retrieve),
        ("score_fit", score),
        ("save", save),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "sanitize")
    order = ["sanitize", "extract_requirements", "retrieve_evidence", "score_fit", "save"]
    for a, b in itertools.pairwise(order):
        g.add_conditional_edges(a, stop_on_error(b), [b, END])
    g.add_edge("save", END)
    graph: CompiledStateGraph[Any, Any, Any, Any] = g.compile()
    return graph


def run_fit(job: JobPosting, deps: Deps) -> FitState:
    graph = build_fit_graph(deps)
    tracer = deps.llm.tracer
    with tracer.run(
        "fit_graph", input={"job_id": job.id, "title": job.title}, company=job.company
    ) as trace:
        out: FitState = graph.invoke(
            {
                "job": job,
                "trace_id": trace.trace_id,  # type: ignore[assignment]
                "trace_root": trace.id,
            }
        )
        fit = out.get("fit")
        trace.update(
            output={"fit_score": fit.fit_score if fit else None, "error": out.get("error")}
        )
    tracer.flush()
    return out
