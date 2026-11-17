"""Mail evals: classification, extraction, matching, injection and quick-add parsing.

Runs evals/mail/cases.yaml (synthetic, committed) through the real mail graph and quick-add
parser with the record/replay CachingProvider, then gates on the thresholds in that file.
`jobfit eval` runs it after the fit suite; CI replays only.

CONSTRAINT: a real labeled set lives in evals/mail/private/ (gitignored) with its OWN cache dir,
because recordings store the start of each prompt and those would be real emails.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from jobfit.applications.match import company_score, role_score
from jobfit.applications.models import Application, MailMessage
from jobfit.applications.quick_add import parse_quick_add
from jobfit.applications.store import InMemoryAppStore
from jobfit.config import ROOT
from jobfit.evals.cache import CACHE_DIR, CachingProvider, Mode
from jobfit.graphs.common import Deps
from jobfit.graphs.mail import build_mail_graph, run_mail
from jobfit.llm.client import LLM, ModelsConfig, build_provider, has_key
from jobfit.llm.cost import InMemoryLedger
from jobfit.profile.loader import load_constraints, load_profile
from jobfit.rag.embedder import HashEmbedder
from jobfit.rag.store import InMemoryStore
from jobfit.store.repo import InMemoryRepo
from jobfit.tracing import MemoryTracer

MAIL_EVALS = ROOT / "evals" / "mail"
PRIVATE = MAIL_EVALS / "private"
QUICK_ADD_TODAY = date(2026, 10, 15)


@dataclass
class MailCaseResult:
    id: str
    expected: dict[str, Any]
    got: dict[str, Any] = field(default_factory=dict)
    kind_ok: bool = False
    company_ok: bool = False
    role_ok: bool = False
    outcome_ok: bool = False
    vague: bool = False
    injection: bool = False
    flagged: bool = False
    error: str | None = None


@dataclass
class QuickAddResult:
    text: str
    expected: dict[str, Any]
    got: dict[str, Any] = field(default_factory=dict)
    fields_ok: int = 0
    fields_total: int = 0
    error: str | None = None


@dataclass
class MailSummary:
    run_at: str
    suite: str
    cases: int
    classification: float
    company: float
    role: float
    outcome: float
    vague_review: float | None
    injection_passed: int
    injection_total: int
    quick_add: float | None
    errored: list[str]
    cache_misses: int
    gate_passed: bool
    gate_failures: list[str]


_UNSET = object()


def _company_ok(want: object, got: str | None) -> bool:
    """Label omits company: not graded. Label says null: the model must return null too."""
    if want is _UNSET:
        return True
    if want is None:
        return not got
    return bool(got) and company_score(str(want), got or "") >= 90


def _role_ok(want: str | None, got: str | None) -> bool:
    if want is None:
        return got is None
    return bool(got) and role_score(want, got or "") >= 80


def _deps(provider: CachingProvider, cfg: ModelsConfig) -> Deps:
    profile = load_profile()
    return Deps(
        llm=LLM(provider, cfg, InMemoryLedger(), MemoryTracer()),
        store=InMemoryStore(HashEmbedder()),  # unused by the mail graph
        repo=InMemoryRepo(),
        profile=profile,
        constraints=load_constraints(),
    )


def run_email_case(
    c: dict[str, Any], seed: dict[str, dict[str, Any]], received: datetime, deps: Deps
) -> MailCaseResult:
    exp = c["expect"]
    res = MailCaseResult(
        c["id"], exp, vague=bool(c.get("vague")), injection=bool(c.get("injection"))
    )
    store = InMemoryAppStore()
    for key, s in seed.items():
        store.save_app(Application(id=key, source="manual", last_activity_at=received, **s))
    msg = MailMessage(
        account="eval",
        message_id=f"<{c['id']}@eval.test>",
        sender=c["from"],
        subject=c["subject"],
        received_at=received,
        body=c["body"],
    )
    try:
        out = run_mail(msg, deps, store, build_mail_graph(deps, store))
    except Exception as e:  # cache miss in replay
        res.error = f"{type(e).__name__}: {str(e)[:160]}"
        return res
    if out.get("error"):
        res.error = str(out["error"])[:200]
        return res
    cls, ev, app = out["cls"], out["event"], out.get("app")
    outcome = out["outcome"]
    if outcome == "matched" and app is not None:
        outcome = f"matched:{app.id}"
    res.got = {"kind": cls.kind, "company": cls.company, "role": cls.role, "outcome": outcome}
    res.flagged = bool(ev.injection_flags)
    res.kind_ok = cls.kind == exp["kind"]
    res.company_ok = _company_ok(exp.get("company", _UNSET), cls.company)
    res.role_ok = "role" not in exp or _role_ok(exp.get("role"), cls.role)
    res.outcome_ok = outcome == exp["outcome"]
    return res


def run_quick_add_case(c: dict[str, Any], deps: Deps) -> QuickAddResult:
    exp = c["expect"]
    res = QuickAddResult(c["text"], exp)
    try:
        d = parse_quick_add(c["text"], deps, QUICK_ADD_TODAY)
    except Exception as e:
        res.error = f"{type(e).__name__}: {str(e)[:160]}"
        res.fields_total = len(exp)
        return res
    res.got = d.model_dump(mode="json")
    checks = {
        "company": _company_ok(exp["company"], d.company),
        "role": _role_ok(exp.get("role"), d.role),
        "salary_entered": (exp.get("salary_entered") or "").lower().replace(" ", "")
        == (d.salary_entered or "").lower().replace(" ", ""),
        "channel": exp.get("channel", "other") == d.channel,
    }
    res.fields_total = len(checks)
    res.fields_ok = sum(checks.values())
    return res


def _rate(xs: list[bool]) -> float:
    return sum(xs) / len(xs) if xs else 1.0


def run_mail_eval(
    mode: Mode = "auto", cases_path: Path | None = None, cache_dir: Path | None = None
) -> tuple[list[MailCaseResult], list[QuickAddResult], MailSummary]:
    path = cases_path or MAIL_EVALS / "cases.yaml"
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = ModelsConfig.load()
    name = cfg.eval_provider  # WHY: same model every run, or recordings miss (see runner.py)
    inner = build_provider(name) if mode != "replay" and has_key(name) else None
    provider = CachingProvider(inner, name, mode, cache_dir=cache_dir or CACHE_DIR)
    deps = _deps(provider, cfg)
    received = datetime.fromisoformat(spec["received_at"])
    seed = spec.get("seed", {})

    emails = [run_email_case(c, seed, received, deps) for c in spec.get("emails", [])]
    quick = [run_quick_add_case(c, deps) for c in spec.get("quick_add", [])]

    ok = [r for r in emails if not r.error]
    th = spec.get("thresholds", {})
    vague = [r.outcome_ok for r in ok if r.vague]
    inj = [r for r in emails if r.injection]
    inj_pass = sum(1 for r in inj if not r.error and r.kind_ok and r.company_ok)
    qa_total = sum(q.fields_total for q in quick)
    s = MailSummary(
        run_at=datetime.now().isoformat(timespec="seconds"),
        suite=str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else path.name,
        cases=len(emails),
        classification=_rate([r.kind_ok for r in ok]),
        company=_rate([r.company_ok for r in ok]),
        role=_rate([r.role_ok for r in ok]),
        outcome=_rate([r.outcome_ok for r in ok]),
        vague_review=_rate(vague) if vague else None,
        injection_passed=inj_pass,
        injection_total=len(inj),
        quick_add=(sum(q.fields_ok for q in quick) / qa_total) if qa_total else None,
        errored=[r.id for r in emails if r.error] + [q.text for q in quick if q.error],
        cache_misses=sum(1 for e in provider.events if e.missed),
        gate_passed=True,
        gate_failures=[],
    )
    fails: list[str] = []
    for metric in ("classification", "company", "outcome", "vague_review", "quick_add"):
        v = getattr(s, metric)
        if v is not None and metric in th and v < th[metric]:
            fails.append(f"{metric} {v:.0%} < {th[metric]:.0%}")
    if s.injection_total and s.injection_passed / s.injection_total < th.get("injection", 1.0):
        fails.append(f"injection {s.injection_passed}/{s.injection_total}")
    if s.errored:
        fails.append(f"{len(s.errored)} errored (cache misses: {s.cache_misses})")
    s.gate_failures, s.gate_passed = fails, not fails
    return emails, quick, s


def markdown(emails: list[MailCaseResult], quick: list[QuickAddResult], s: MailSummary) -> str:
    def pct(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.0%}"

    lines = [
        "# Mail eval report",
        "",
        f"Run {s.run_at} on `{s.suite}`. Gate: **{'PASS' if s.gate_passed else 'FAIL'}**"
        + (f" ({'; '.join(s.gate_failures)})" if s.gate_failures else ""),
        "",
        "| Metric | Result |",
        "| --- | --- |",
        f"| Classification accuracy | {pct(s.classification)} |",
        f"| Company extraction | {pct(s.company)} |",
        f"| Role extraction (not gated) | {pct(s.role)} |",
        f"| Match outcome | {pct(s.outcome)} |",
        f"| Vague role sent to review / linked safely | {pct(s.vague_review)} |",
        f"| Injection cases passed | {s.injection_passed}/{s.injection_total} |",
        f"| Quick-add fields | {pct(s.quick_add)} |",
        "",
        "## Misses",
        "",
    ]
    misses = [r for r in emails if r.error or not (r.kind_ok and r.company_ok and r.outcome_ok)]
    if not misses:
        lines.append("None.")
    for r in misses:
        lines.append(f"- `{r.id}`: expected {r.expected}, got {r.got or r.error}")
    bad_q = [q for q in quick if q.error or q.fields_ok < q.fields_total]
    for q in bad_q:
        lines.append(f"- quick-add `{q.text}`: expected {q.expected}, got {q.got or q.error}")
    return "\n".join(lines) + "\n"


def write_report(
    emails: list[MailCaseResult], quick: list[QuickAddResult], s: MailSummary, out: Path
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(markdown(emails, quick, s), encoding="utf-8")
    (out / "report.json").write_text(
        json.dumps(
            {
                "summary": asdict(s),
                "emails": [asdict(r) for r in emails],
                "quick_add": [asdict(q) for q in quick],
            },
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )
