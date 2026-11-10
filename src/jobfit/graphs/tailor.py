"""Tailor graph, run only after Tylor approves a job:

  load_job -> plan_emphasis -> draft_resume -> draft_cover_letter -> verify -> human_review
                                    ^                  ^                 |            |
                                    +---- retry (max 2, with reasons) --+            |
                                    +---- "revise" with Tylor's note -----------------+
                                                         human_review --approve--> render_pdfs

human_review is a LangGraph interrupt. Resume it with Command(resume={...}):
  {"action": "approve"}
  {"action": "edit", "resume": ResumeOut-dict?, "cover_letter": CoverLetterOut-dict?}
  {"action": "revise", "note": "..."}
Edits are re-checked (no auto-redraft) and shown again. Approval renders unless a hard rule
(em dash, banned phrase, forbidden tool, degree claim, page count) is still broken.

Entry points: start_tailor(job_id, deps, checkpointer) and resume_tailor(...).
"""

from __future__ import annotations

import json
import operator
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel

from jobfit.config import ROOT
from jobfit.graphs import prompts
from jobfit.graphs.common import Deps, State, node
from jobfit.graphs.verify import (
    HARD_RULES,
    hallucination_count,
    verify_cover_letter,
    verify_resume,
)
from jobfit.schemas import (
    ClaimCheckOut,
    CoverLetterOut,
    Drafts,
    EmphasisOut,
    FitResult,
    Issue,
    JobPosting,
    ResumeOut,
)

MAX_RETRIES = 2

# WHY: models emit typographic look-alikes (U+2011 non-breaking hyphen, U+00A0, U+202F) that the
# Roboto Latin subset may not have, and that break plain-text search in the PDF. The em dash is
# deliberately NOT mapped: verify must still see and reject it.
_LOOKALIKES = str.maketrans({"\u2010": "-", "\u2011": "-", "\u00a0": " ", "\u202f": " "})


def clean_text(s: str) -> str:
    return s.translate(_LOOKALIKES)


def clean_model[M: BaseModel](m: M) -> M:
    """Apply clean_text to every string in a draft model."""

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            return clean_text(v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    return type(m).model_validate(walk(m.model_dump()))


OUTPUT_DIR = ROOT / "output"


class TailorState(TypedDict, total=False):
    job_id: str
    thread_id: str
    trace_id: str
    trace_root: str | None
    job: JobPosting
    fit: FitResult
    plan: EmphasisOut
    resume: ResumeOut
    cover_letter: CoverLetterOut
    resume_feedback: list[str]
    letter_feedback: list[str]
    issues: list[Issue]
    retries: int
    mode: Literal["auto", "human"]  # human = re-check edits without redrafting
    review: dict[str, Any]
    pdfs: list[str]
    # One entry per verify pass, appended (operator.add): lets evals and the dashboard show how
    # many unsupported claims the first draft had and how many the loop removed.
    verify_log: Annotated[list[dict[str, Any]], operator.add]
    error: str
    error_node: str


def _catalog(deps: Deps, plan: EmphasisOut | None = None) -> str:
    """Compact profile listing for the drafting prompts: IDs and text only."""
    p = deps.profile
    keep = set(plan.bullet_ids) if plan else None
    lines = ["<profile>", f"[sum.1] {p.summary[0].text}", "Skills:"]
    lines += [f"  [{s.id}] {s.name}" for s in p.all_skills()]
    roles = [(j.id, j.title, j.company, j.start, j.end, j.bullets) for j in p.experience]
    roles += [(m.id, m.title, m.branch, m.start, m.end, m.bullets) for m in p.military]
    for rid, title, org, start, end, bullets in roles:
        lines.append(f"Role [{rid}] {title} | {org} ({start} to {end})")
        for b in bullets:
            mark = "" if keep is None or b.id in keep else " (lower priority)"
            lines.append(f"  [{b.id}]{mark} {b.text}")
    edu = "; ".join(deps.profile.evidence_index()[e.id] for e in p.education)
    lines += [f"Education: {edu}", "</profile>"]
    return "\n".join(lines)


def _job_brief(job: JobPosting, fit: FitResult) -> str:
    lines = [f"Job: {job.title} at {job.company}", "<requirements>"]
    for a in fit.assessments:
        ids = ", ".join(e.id for e in a.evidence) or "none"
        lines.append(f"- ({a.requirement.kind}) {a.requirement.text} -> {a.status} [{ids}]")
    lines += ["</requirements>", f"Top gaps: {'; '.join(fit.top_gaps) or 'none'}"]
    return "\n".join(lines)


def _feedback(items: list[str] | None, previous: BaseModel | None = None) -> str:
    if not items:
        return ""
    out = "\n\nFeedback from the last check. Fix all of these:\n" + "\n".join(
        f"- {i}" for i in items
    )
    if previous is not None:
        # WHY: a from-scratch redraft fixes the listed problems and invents new ones (seen in
        # evals: a word-count fix brought 4 unsupported numbers). Editing converges.
        out += (
            "\n\n<previous_draft>\n" + previous.model_dump_json() + "\n</previous_draft>\n"
            "Return the previous draft with only these problems fixed. Keep every other line "
            "as it is unless a fix requires changing it. To fix an unsupported claim, delete it "
            "or reword it to say only what the cited evidence says."
        )
    return out


def build_tailor_graph(
    deps: Deps, checkpointer: BaseCheckpointSaver[Any] | None
) -> CompiledStateGraph[Any, Any, Any, Any]:
    @node("load_job", deps)
    def load_job(state: State) -> State:
        job = deps.repo.get_job(state["job_id"])
        fit = deps.repo.get_fit(state["job_id"])
        if job is None or fit is None:
            return {"error": f"job {state['job_id']} not found or not scored yet"}
        return {"job": job, "fit": fit, "retries": 0, "mode": "auto"}

    @node("plan_emphasis", deps)
    def plan_emphasis(state: State) -> State:
        user = f"{_job_brief(state['job'], state['fit'])}\n\n{_catalog(deps)}"
        res = deps.llm.structured("plan_emphasis", prompts.PLAN_EMPHASIS, user, EmphasisOut)
        idx = deps.profile.evidence_index()
        plan = res.output.model_copy(
            update={
                "skill_ids": [i for i in res.output.skill_ids if i in idx],
                "bullet_ids": [i for i in res.output.bullet_ids if i in idx],
            }
        )
        return {"plan": plan}

    @node("draft_resume", deps)
    def draft_resume(state: State) -> State:
        if state.get("resume") and not state.get("resume_feedback"):
            return {}  # passed last check; only the letter is being redrafted
        plan: EmphasisOut = state["plan"]
        user = (
            f"{_job_brief(state['job'], state['fit'])}\n\nAngle: {plan.angle}\n"
            f"Feature skills: {', '.join(plan.skill_ids)}\n"
            f"Keep bullets (priority order): {', '.join(plan.bullet_ids)}\n\n"
            f"{_catalog(deps, plan)}{_feedback(state.get('resume_feedback'), state.get('resume'))}"
        )
        res = deps.llm.structured("draft_resume", prompts.DRAFT_RESUME, user, ResumeOut)
        return {"resume": clean_model(res.output), "resume_feedback": []}

    @node("draft_cover_letter", deps)
    def draft_cover_letter(state: State) -> State:
        if state.get("cover_letter") and not state.get("letter_feedback"):
            return {}
        plan: EmphasisOut = state["plan"]
        idx = deps.profile.evidence_index()
        evidence = "\n".join(f"[{i}] {idx[i]}" for i in plan.bullet_ids[:8] if i in idx)
        voice = "\n\n---\n\n".join(deps.voice_samples[:2])
        user = (
            f"{_job_brief(state['job'], state['fit'])}\n\nAngle: {plan.angle}\n"
            f"Posting hooks: {'; '.join(plan.posting_hooks)}\n\n"
            f"<evidence>\n{evidence}\n[sum.1] {idx['sum.1']}\n</evidence>\n\n"
            + (f"<voice_samples>\n{voice}\n</voice_samples>\n" if voice else "")
            + _feedback(state.get("letter_feedback"), state.get("cover_letter"))
        )
        res = deps.llm.structured(
            "draft_cover_letter", prompts.DRAFT_COVER_LETTER, user, CoverLetterOut
        )
        return {"cover_letter": clean_model(res.output), "letter_feedback": []}

    def _llm_claim_check(resume: ResumeOut, letter: CoverLetterOut) -> list[Issue]:
        idx = deps.profile.evidence_index()
        items: list[tuple[Literal["resume", "cover_letter"], str, list[str]]] = [
            ("resume", resume.summary, resume.summary_evidence_ids)
        ]
        items += [("resume", b.text, b.source_ids) for j in resume.jobs for b in j.bullets]
        # A gap paragraph names what Tylor lacks; it is not a claim to fact-check.
        items += [
            ("cover_letter", p.text, p.evidence_ids)
            for p in letter.paragraphs
            if p.evidence_ids and p.kind != "gap"
        ]
        prompt = "\n\n".join(
            f"[{n}] CLAIM: {text}\nEVIDENCE: {' | '.join(idx.get(i, '?') for i in ids)}"
            for n, (_, text, ids) in enumerate(items)
        )
        res = deps.llm.structured("verify_claims", prompts.VERIFY_CLAIMS, prompt, ClaimCheckOut)
        return [
            Issue(
                doc=items[u.item][0] if 0 <= u.item < len(items) else "resume",
                rule="unsupported_claim",
                detail=f"{u.claim} ({u.reason})",
            )
            for u in res.output.unsupported
        ]

    @node("verify", deps)
    def verify(state: State) -> State:
        resume: ResumeOut = state["resume"]
        letter: CoverLetterOut = state["cover_letter"]
        employer = [state["job"].company]
        issues = verify_resume(
            resume, deps.profile, deps.constraints, deps.page_count, allow_terms=employer
        )
        issues += verify_cover_letter(
            letter, deps.profile, deps.constraints, state["fit"], allow_terms=employer
        )
        # The paid semantic check only runs once the free deterministic ones pass.
        if not issues and deps.verify_with_llm:
            issues += _llm_claim_check(resume, letter)
        out: State = {
            "issues": issues,
            "verify_log": [
                {
                    "round": state.get("retries", 0),
                    "mode": state.get("mode", "auto"),
                    "issues": len(issues),
                    "hallucinations": hallucination_count(issues),
                    "rules": sorted({i.rule for i in issues}),
                }
            ],
        }
        if issues and state.get("mode") == "auto" and state.get("retries", 0) < MAX_RETRIES:
            out["retries"] = state.get("retries", 0) + 1
            out["resume_feedback"] = [f"{i.rule}: {i.detail}" for i in issues if i.doc == "resume"]
            out["letter_feedback"] = [
                f"{i.rule}: {i.detail}" for i in issues if i.doc == "cover_letter"
            ]
        return out

    def after_verify(state: State) -> str:
        if state.get("error"):
            return END
        if state.get("resume_feedback"):
            return "draft_resume"
        if state.get("letter_feedback"):
            return "draft_cover_letter"
        return "human_review"  # passed, or out of retries: surface to Tylor, don't guess

    @node("human_review", deps)
    def human_review(state: State) -> State:
        deps.repo.save_tailor(
            state["job_id"],
            state.get("thread_id", ""),
            "awaiting_review",
            drafts=Drafts(resume=state["resume"], cover_letter=state["cover_letter"]),
            issues=[i.model_dump() for i in state.get("issues", [])],
        )
        decision: dict[str, Any] = interrupt(
            {
                "job_id": state["job_id"],
                "resume": state["resume"].model_dump(),
                "cover_letter": state["cover_letter"].model_dump(),
                "issues": [i.model_dump() for i in state.get("issues", [])],
                "retries_used": state.get("retries", 0),
            }
        )
        return {"review": decision}

    def after_review(state: State) -> Command[str]:
        d = state.get("review") or {}
        action = d.get("action")
        if action == "edit":
            upd: dict[str, Any] = {"mode": "human", "resume_feedback": [], "letter_feedback": []}
            if d.get("resume"):
                upd["resume"] = ResumeOut.model_validate(d["resume"])
            if d.get("cover_letter"):
                upd["cover_letter"] = CoverLetterOut.model_validate(d["cover_letter"])
            return Command(goto="verify", update=upd)
        if action == "revise":
            note = f"Reviewer note: {d.get('note', '').strip()}"
            return Command(
                goto="draft_resume",
                update={
                    "mode": "auto",
                    "retries": 0,
                    "resume_feedback": [note],
                    "letter_feedback": [note],
                },
            )
        if action == "approve":
            blocking = [i for i in state.get("issues", []) if i.rule in HARD_RULES]
            if blocking:
                # Hard rules hold even on approval. Show them again instead of rendering.
                return Command(goto="human_review", update={"issues": blocking})
            return Command(goto="render_pdfs")
        return Command(goto="human_review")  # unknown action: ask again

    @node("render_pdfs", deps)
    def render_pdfs(state: State) -> State:
        if deps.render is None:
            return {"error": "no renderer configured"}
        try:
            resume_path, cover_path = deps.render(
                state["resume"], state["cover_letter"], state["job"]
            )
        except RuntimeError as e:  # e.g. Chromium missing, font load failure, 2-page resume
            return {"error": f"render: {e}"}
        deps.repo.save_tailor(
            state["job_id"],
            state.get("thread_id", ""),
            "rendered",
            resume_pdf=resume_path,
            cover_pdf=cover_path,
        )
        return {"pdfs": [resume_path, cover_path]}

    g: Any = StateGraph(TailorState)
    g.add_node("load_job", load_job)
    g.add_node("plan_emphasis", plan_emphasis)
    g.add_node("draft_resume", draft_resume)
    g.add_node("draft_cover_letter", draft_cover_letter)
    g.add_node("verify", verify)
    g.add_node("human_review", human_review)
    g.add_node(
        "review_router",
        after_review,
        destinations=("verify", "draft_resume", "human_review", "render_pdfs"),
    )
    g.add_node("render_pdfs", render_pdfs)

    def ok(nxt: str) -> Any:
        return lambda s: END if s.get("error") else nxt

    g.add_edge(START, "load_job")
    g.add_conditional_edges("load_job", ok("plan_emphasis"), ["plan_emphasis", END])
    g.add_conditional_edges("plan_emphasis", ok("draft_resume"), ["draft_resume", END])
    g.add_conditional_edges("draft_resume", ok("draft_cover_letter"), ["draft_cover_letter", END])
    g.add_conditional_edges("draft_cover_letter", ok("verify"), ["verify", END])
    g.add_conditional_edges(
        "verify", after_verify, ["draft_resume", "draft_cover_letter", "human_review", END]
    )
    g.add_edge("human_review", "review_router")
    g.add_edge("render_pdfs", END)
    graph: CompiledStateGraph[Any, Any, Any, Any] = g.compile(checkpointer=checkpointer)
    return graph


def _invoke(
    graph: Any,
    payload: Any,
    thread_id: str,
    deps: Deps,
    job_id: str,
) -> dict[str, Any]:
    cfg = {"configurable": {"thread_id": thread_id}}
    with deps.llm.tracer.run(
        "tailor_graph", input={"job_id": job_id, "thread_id": thread_id}, session_id=thread_id
    ) as trace:
        if isinstance(payload, dict):
            payload |= {"trace_id": trace.trace_id, "trace_root": trace.id}
        out: dict[str, Any] = graph.invoke(payload, cfg)
        status = (
            "awaiting_review"
            if out.get("__interrupt__")
            else "failed"
            if out.get("error")
            else "rendered"
        )
        trace.update(output={"status": status, "error": out.get("error")})
        url = deps.llm.tracer.url(trace.trace_id)
    deps.llm.tracer.flush()
    if out.get("error"):
        deps.repo.save_tailor(
            job_id,
            thread_id,
            "failed",
            issues=[{"doc": "resume", "rule": "run_error", "detail": out["error"]}],
        )
    elif url:
        deps.repo.save_tailor(job_id, thread_id, status, trace_url=url)
    out["thread_id"] = thread_id
    out["status"] = status
    return out


def start_tailor(
    job_id: str, deps: Deps, checkpointer: BaseCheckpointSaver[Any] | None
) -> dict[str, Any]:
    thread_id = f"tailor-{job_id}-{uuid.uuid4().hex[:8]}"
    graph = build_tailor_graph(deps, checkpointer)
    return _invoke(graph, {"job_id": job_id, "thread_id": thread_id}, thread_id, deps, job_id)


def resume_tailor(
    job_id: str,
    thread_id: str,
    decision: dict[str, Any],
    deps: Deps,
    checkpointer: BaseCheckpointSaver[Any],
) -> dict[str, Any]:
    graph = build_tailor_graph(deps, checkpointer)
    return _invoke(graph, Command(resume=decision), thread_id, deps, job_id)


def load_voice_samples() -> list[str]:
    d = ROOT / "profile" / "voice"
    return [p.read_text() for p in sorted(d.glob("*.txt"))] if d.exists() else []


def make_file_renderer(deps: Deps, out_dir: Path = OUTPUT_DIR) -> Any:
    """Build the render callable for Deps.render (Playwright + templates)."""
    from jobfit.render.pdf import cover_letter_html, default_renderer, output_names, resume_html

    def render(resume: ResumeOut, letter: CoverLetterOut, job: JobPosting) -> tuple[str, str]:
        r = default_renderer()
        out_dir.mkdir(parents=True, exist_ok=True)
        res = r.render(resume_html(resume, deps.profile, job))
        if res.pages > deps.constraints.resume.max_pages:
            raise RuntimeError(f"resume renders to {res.pages} pages")
        let = r.render(cover_letter_html(letter, deps.profile, job))
        rn, cn = output_names(deps.profile, job)
        (out_dir / rn).write_bytes(res.pdf)
        (out_dir / cn).write_bytes(let.pdf)
        (out_dir / f"{rn[:-4]}.json").write_text(
            json.dumps(
                {"resume": resume.model_dump(), "cover_letter": letter.model_dump()}, indent=2
            )
        )
        return str(out_dir / rn), str(out_dir / cn)

    return render


def make_page_counter(deps: Deps) -> Any:
    from jobfit.render.pdf import default_renderer, resume_html

    stub = JobPosting(
        id="x", source="x", external_id="x", company="Company", title="Role", description=""
    )

    def count(resume: ResumeOut) -> int:
        return default_renderer().render(resume_html(resume, deps.profile, stub)).pages

    return count
