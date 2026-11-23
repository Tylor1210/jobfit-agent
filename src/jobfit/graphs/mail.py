"""Mail graph: one fetched email -> a classified event -> a matched or new application.

sanitize -> classify -> match -> apply. Called by mail/sync.py once per message not seen before
(the caller dedups by Message-ID first, so nothing is paid twice) and by the mail evals.

CONSTRAINT: the email body is untrusted. It is sanitized with the posting injection guard,
wrapped in <email> tags, kept out of traces (`private=True`), and never stored. Only the
extracted fields and a <=200 char snippet reach the database.
CONSTRAINT: a role is never guessed. See applications/match.py.
"""

from __future__ import annotations

import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from jobfit.applications.match import MatchResult, match_application, match_job
from jobfit.applications.models import (
    SNIPPET_MAX,
    AppEvent,
    Application,
    MailClassOut,
    MailMessage,
)
from jobfit.applications.status import next_status
from jobfit.applications.store import APP_TO_STAGE, AppStore, stage_after
from jobfit.graphs import prompts
from jobfit.graphs.common import Deps, State, node, stop_on_error
from jobfit.graphs.sanitize import sanitize, wrap

BODY_MAX = 4000  # WHY: the gist of a recruiting email is in its first screen; saves tokens
LOW_CONFIDENCE = 0.6
STATUS_KINDS = {"application_received", "rejection", "interview_request", "assessment", "offer"}
FOLLOW_UP_KINDS = {"rejection", "interview_request", "assessment", "offer"}

# LinkedIn confirmations (incl. Easy Apply) are classified in code: deterministic and free.
_LI_SENT = re.compile(r"your application was sent to (?P<company>[^\n.!]+)", re.I)
_LI_APPLIED = re.compile(r"you applied (?:for|to) (?P<role>[^\n]+?) at (?P<company>[^\n.!]+)", re.I)


class MailState(TypedDict, total=False):
    msg: MailMessage
    trace_id: str
    trace_root: str | None
    clean: str  # sanitized body, in memory only
    flags: list[str]
    snippet: str
    cls: MailClassOut
    fast_path: bool
    match: MatchResult
    event: AppEvent
    app: Application | None
    outcome: str  # matched | created | follow_up | review | event_only
    error: str
    error_node: str


def _domain(sender: str) -> str:
    m = re.search(r"@([\w.-]+)", sender)
    return (m.group(1) if m else sender).lower()


def linkedin_confirmation(msg: MailMessage) -> MailClassOut | None:
    if not _domain(msg.sender).endswith("linkedin.com"):
        return None
    text = f"{msg.subject}\n{msg.body}"
    if m := _LI_APPLIED.search(text):
        company, role = m.group("company").strip(), m.group("role").strip()
    elif m := _LI_SENT.search(text):
        company, role = m.group("company").strip(), _li_role(msg.body, m.group("company").strip())
    else:
        return None
    return MailClassOut(
        kind="application_received",
        company=company,
        role=role,
        next_step=None,
        deadline=None,
        confidence=0.95,
    )


def _li_role(body: str, company: str) -> str | None:
    """LinkedIn puts the job title on the line right above the company name. Anything less
    certain returns None (role unknown) rather than a guess."""
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    for i in range(1, len(lines)):
        if lines[i].lower().startswith(company.lower()) and "sent to" not in lines[i - 1].lower():
            cand = lines[i - 1]
            if 3 <= len(cand) <= 90 and not cand.lower().startswith(("your application", "view")):
                return cand
    return None


def snippet_for(msg: MailMessage, clean: str) -> str:
    first = " ".join(clean.split())[:140]
    return f"{msg.subject.strip()} | {first}"[:SNIPPET_MAX]


def build_mail_graph(deps: Deps, store: AppStore) -> CompiledStateGraph[Any, Any, Any, Any]:
    @node("sanitize", deps, private=True)
    def sanitize_node(state: State) -> State:
        msg: MailMessage = state["msg"]
        s = sanitize(msg.body)
        clean = s.text[:BODY_MAX]
        return {"clean": clean, "flags": s.flags, "snippet": snippet_for(msg, clean)}

    @node("classify_mail", deps)
    def classify(state: State) -> State:
        msg: MailMessage = state["msg"]
        if fast := linkedin_confirmation(msg):
            return {"cls": fast, "fast_path": True}
        user = (
            f"From domain: {_domain(msg.sender)}\nSubject: {msg.subject[:200]}\n"
            f"Received: {msg.received_at.date().isoformat()}\n\n{wrap(state['clean'], 'email')}"
        )
        res = deps.llm.structured(
            "classify_mail", prompts.CLASSIFY_MAIL, user, MailClassOut, private=True
        )
        return {"cls": res.output, "fast_path": False}

    @node("match", deps)
    def match(state: State) -> State:
        cls: MailClassOut = state["cls"]
        return {"match": match_application(cls.company, cls.role, store.list_apps())}

    @node("apply", deps)
    def apply(state: State) -> State:
        return apply_classification(
            store,
            state["msg"],
            state["cls"],
            state["match"],
            snippet=state["snippet"],
            flags=state["flags"],
            fast_path=state.get("fast_path", False),
        )

    g: Any = StateGraph(MailState)
    g.add_node("sanitize", sanitize_node)
    g.add_node("classify", classify)
    g.add_node("match", match)
    g.add_node("apply", apply)
    g.add_edge(START, "sanitize")
    g.add_conditional_edges("sanitize", stop_on_error("classify"))
    g.add_conditional_edges("classify", stop_on_error("match"))
    g.add_conditional_edges("match", stop_on_error("apply"))
    g.add_edge("apply", END)
    compiled: CompiledStateGraph[Any, Any, Any, Any] = g.compile()
    return compiled


def apply_classification(
    store: AppStore,
    msg: MailMessage,
    cls: MailClassOut,
    m: MatchResult,
    *,
    snippet: str,
    flags: list[str],
    fast_path: bool = False,
) -> State:
    """Turn a classification + match into an event and an application change. Pure rules over
    the store; kept outside the graph so evals and tests can call it directly."""
    company = (cls.company or "").strip() or None
    role = (cls.role or "").strip() or None
    confidence = max(0.0, min(1.0, cls.confidence))
    ev = AppEvent(
        kind=cls.kind,
        company=company,
        occurred_at=msg.received_at,
        account=msg.account,
        message_id=msg.message_id,
        snippet=snippet[:SNIPPET_MAX],
        extracted={
            "role": role,
            "next_step": cls.next_step,
            "deadline": cls.deadline,
            "sender_domain": _domain(msg.sender),
            "fast_path": fast_path,
        },
        confidence=confidence,
        injection_flags=flags,
    )
    low = confidence < LOW_CONFIDENCE
    outcome: str
    app: Application | None = None

    if cls.kind not in STATUS_KINDS:
        # recruiter_outreach / other: an event only, never an application.
        if m.kind in ("matched", "company_only") and m.app:
            ev.application_id = m.app.id
        outcome = "event_only"
    elif not company:
        ev.needs_review, ev.review_reason = True, "no company in the email"
        outcome = "review"
    elif m.kind in ("matched", "company_only") and m.app:
        app = m.app.model_copy()
        app.status = next_status(app.status, cls.kind)
        app.last_activity_at = max(app.last_activity_at, msg.received_at)
        if role and not app.role:
            app.role = role
        outcome = "matched"
    elif m.kind == "ambiguous":
        # Several applications at this company and the email doesn't say which. Don't guess.
        n = len(m.candidates)
        ev.needs_review = True
        ev.review_reason = f"role not stated; {n} applications at {company}"
        outcome = "review"
    else:
        is_follow_up = cls.kind in FOLLOW_UP_KINDS
        app = Application(
            company=company,
            role=role,
            status=next_status("applied", cls.kind),
            source="follow_up" if is_follow_up else "email",
            channel="linkedin" if _domain(msg.sender).endswith("linkedin.com") else "other",
            applied_at=None if is_follow_up else msg.received_at,
            last_activity_at=msg.received_at,
        )
        if is_follow_up and not role:
            app.needs_review, app.review_reason = True, "found via follow-up; role not stated"
        outcome = "follow_up" if is_follow_up else "created"

    if app is not None:
        if low:
            app.needs_review = True
            app.review_reason = app.review_reason or "low-confidence classification"
            ev.needs_review, ev.review_reason = True, "low-confidence classification"
        link_job(store, app)
        store.save_app(app)
        ev.application_id = app.id
    store.add_event(ev)
    return {"event": ev, "app": app, "outcome": outcome}


def link_job(store: AppStore, app: Application) -> None:
    """Link the application to a posting on the board (if one matches) and move that card
    forward to match the application's status. Email is the source of truth."""
    jobs = store.job_candidates()
    if app.job_id is None and (j := match_job(app.company, app.role, jobs)):
        app.job_id = j["id"]
    if app.job_id is None:
        return
    target = APP_TO_STAGE.get(app.status)
    cur = next((j["stage"] for j in jobs if j["id"] == app.job_id), None)
    if target and cur and (new := stage_after(cur, target)):
        store.set_job_stage(app.job_id, new)


def run_mail(msg: MailMessage, deps: Deps, store: AppStore, graph: Any = None) -> State:
    """Process one message. Returns the final state; `error` is set on a cost cap, quota stop,
    or provider failure (the message stays unprocessed and is retried on the next run)."""
    g = graph or build_mail_graph(deps, store)
    tracer = deps.llm.tracer
    # Trace metadata carries the account label only: no subject, sender or company.
    with tracer.run("mail_graph", input={"account": msg.account}) as trace:
        out: State = g.invoke({"msg": msg, "trace_id": trace.trace_id, "trace_root": trace.id})
        trace.update(output={"outcome": out.get("outcome"), "error": out.get("error")})
    return out
