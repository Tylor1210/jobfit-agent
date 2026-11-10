"""Write eval outputs: evals/report.json (full, gitignored), evals/report.md (committed summary
for the README), evals/history.jsonl (one line per run, committed; the Ops page trend line),
and optionally an eval_runs row in Postgres (db/migrations/004_evals.sql).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from jobfit.config import ROOT
from jobfit.evals.runner import CaseResult, Summary, to_json

EVALS = ROOT / "evals"


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.0f}%"


def markdown(results: list[CaseResult], s: Summary) -> str:
    gate = "PASS" if s.gate_passed else "FAIL"
    lines = [
        "# Eval report",
        "",
        f"Run {s.run_at} on `{s.provider}` ({s.mode} mode, {_pct(s.cache_hit_rate)} replayed "
        f"from cache). Gate: **{gate}**.",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Verdict accuracy (exact) | {_pct(s.verdict_accuracy)} |",
        f"| Verdict within one band | {_pct(s.verdict_within_one)} |",
        f"| Gap recall | {_pct(s.gap_recall)} |",
        f"| Hallucinated claims in final drafts (target 0) | {s.hallucinated_claims} |",
        f"| Unsupported claims caught by verify (first drafts) | {s.first_draft_hallucinations} |",
        f"| Injection cases passed | {s.injection_passed}/{s.injection_total} |",
        f"| Total LLM latency | {s.total_latency_ms / 1000:.1f}s |",
        f"| Tokens (in / out) | {s.total_input_tokens:,} / {s.total_output_tokens:,} |",
        f"| Cost (list price) | ${s.total_cost_usd:.4f} |",
        "",
        "| Case | Expected | Got | Score | Gaps found | Injection | Drafts (first / final) | "
        "Latency | Cost |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.status == "skipped":
            lines.append(f"| {r.id} | {r.expected_verdict} | skipped ({r.error}) | | | | | | |")
            continue
        if r.status == "error":
            lines.append(f"| {r.id} | {r.expected_verdict} | error | | | | | | |")
            continue
        mark = "" if r.verdict_ok else (" (near)" if r.within_one else " (miss)")
        gaps = f"{r.gaps_found}/{r.gaps_expected}" if r.gaps_expected else "-"
        inj = (
            "-"
            if not r.injection
            else (
                ("pass" if r.injection_ok else "FAIL")
                + (", flagged" if r.injection_flagged else ", not flagged")
            )
        )
        drafts = f"{r.first_draft_hallucinations} / {r.final_hallucinations}" if r.tailored else "-"
        lines.append(
            f"| {r.id} | {r.expected_verdict} | {r.verdict}{mark} | {r.fit_score} | "
            f"{gaps} | {inj} | {drafts} | {r.latency_ms / 1000:.1f}s | "
            f"${r.cost_usd:.4f} |"
        )
    if s.cases_skipped:
        lines += ["", f"Skipped (no posting text yet): {', '.join(s.cases_skipped)}."]
    if not s.page_check:
        lines += ["", "One-page resume check skipped (Chromium not installed)."]
    missed = [(r.id, r.gaps_missed) for r in results if r.gaps_missed]
    if missed:
        lines += ["", "Missed gaps: " + "; ".join(f"{i}: {', '.join(g)}" for i, g in missed)]
    if s.gate_failures:
        lines += ["", "Gate failures:", *[f"- {f}" for f in s.gate_failures]]
    return "\n".join(lines) + "\n"


def write_reports(results: list[CaseResult], s: Summary, out: Path = EVALS) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(to_json(results, s), indent=2), encoding="utf-8")
    (out / "report.md").write_text(markdown(results, s), encoding="utf-8")
    # WHY: replay runs repeat a recorded run; adding them would flatten the trend line.
    if s.mode != "replay":
        with (out / "history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(s)) + "\n")


def save_to_db(dsn: str, results: list[CaseResult], s: Summary) -> None:
    from jobfit.store.db import connect

    with connect(dsn) as c:
        c.execute(
            "insert into eval_runs (run_at, summary, cases) values (%s, %s, %s)",
            (s.run_at, json.dumps(asdict(s)), json.dumps([asdict(r) for r in results])),
        )
