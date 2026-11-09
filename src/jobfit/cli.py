"""Command line entry point (`jobfit ...`, installed via pyproject [project.scripts]).

jobfit profile validate      integrity-check profile + constraints (no network)
jobfit db migrate            apply db/migrations/*.sql to DATABASE_URL in order
jobfit profile index         embed profile chunks into pgvector (skips unchanged chunks)
jobfit profile search "q"    query the evidence store, for sanity checks
"""

from __future__ import annotations

import argparse
import sys

from jobfit.config import ROOT, get_settings
from jobfit.profile.chunks import build_chunks
from jobfit.profile.loader import load_profile
from jobfit.rag.embedder import get_embedder
from jobfit.rag.store import PgVectorStore


def _dsn() -> str:
    dsn = get_settings().database_url
    if not dsn:
        sys.exit("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    return dsn


def _migrate() -> None:
    import psycopg

    files = sorted((ROOT / "db" / "migrations").glob("*.sql"))
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        for f in files:
            # WHY: every migration is idempotent (if not exists), so re-running is safe and we
            # avoid a migrations table until there is a migration that needs ordering guarantees.
            conn.execute(f.read_text(encoding="utf-8"))
            print(f"applied {f.name}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="jobfit")
    sub = ap.add_subparsers(dest="group", required=True)
    prof = sub.add_parser("profile").add_subparsers(dest="cmd", required=True)
    prof.add_parser("validate")
    prof.add_parser("index")
    search = prof.add_parser("search")
    search.add_argument("query")
    search.add_argument("-k", type=int, default=5)
    db = sub.add_parser("db").add_subparsers(dest="cmd", required=True)
    db.add_parser("migrate")
    fit = sub.add_parser("fit")
    fit.add_argument("posting", help="path to a text file with the posting")
    fit.add_argument("--company", required=True)
    fit.add_argument("--title", required=True)
    fit.add_argument("--location")
    fit.add_argument("--tailor", action="store_true")
    args = ap.parse_args(argv)

    if args.group == "db":
        _migrate()
        return
    if args.group == "fit":
        _fit(args)
        return

    profile = load_profile()
    if args.cmd == "validate":
        chunks = build_chunks(profile)
        print(f"ok: {len(profile.evidence_index())} evidence ids, {len(chunks)} chunks")
    elif args.cmd == "index":
        n = PgVectorStore(_dsn(), get_embedder()).upsert(build_chunks(profile))
        print(f"embedded {n} new/changed chunks")
    elif args.cmd == "search":
        for h in PgVectorStore(_dsn(), get_embedder()).search(args.query, k=args.k):
            print(f"{h.score:.3f}  {h.id:<28} {h.text[:90]}")


def _fit(args: argparse.Namespace) -> None:
    import hashlib
    import json
    from pathlib import Path

    from jobfit.app import build_checkpointer, build_deps
    from jobfit.graphs.fit import run_fit
    from jobfit.graphs.tailor import resume_tailor, start_tailor
    from jobfit.schemas import JobPosting

    text = Path(args.posting).read_text(encoding="utf-8")
    ext = hashlib.sha256(f"{args.company}|{args.title}".encode()).hexdigest()[:12]
    job = JobPosting(
        id=f"manual:{ext}",
        source="manual",
        external_id=ext,
        company=args.company,
        title=args.title,
        location=args.location,
        description=text,
    )
    deps = build_deps(render=args.tailor)
    out = run_fit(job, deps)
    if out.get("error"):
        sys.exit(f"stopped: {out['error']}")
    f = out["fit"]
    print(f"\n{f.fit_score}/100  {f.verdict}  {f.verdict_line}")
    for a in f.assessments:
        ids = ",".join(e.id for e in a.evidence)
        print(f"  {a.status:<8} ({a.requirement.kind[:3]}) {a.requirement.text[:70]:<70} {ids}")
    print("gaps:", "; ".join(f.top_gaps))
    if f.injection_flags:
        print("INJECTION FLAGS:", *f.injection_flags, sep="\n  ")
    print(f"spend today: ${deps.llm.ledger.spent_today():.4f}")
    if not args.tailor:
        return

    saver, closer = build_checkpointer()
    try:
        res = start_tailor(job.id, deps, saver)
        while res["status"] == "awaiting_review":
            payload = res["__interrupt__"][0].value
            print(json.dumps({k: payload[k] for k in ("resume", "cover_letter")}, indent=2))
            for i in payload["issues"]:
                print(f"ISSUE [{i['doc']}] {i['rule']}: {i['detail']}")
            choice = input("\n[a]pprove, [r]evise with a note, [q]uit: ").strip().lower()
            if choice.startswith("q"):
                print(f"paused. thread: {res['thread_id']}")
                return
            decision = (
                {"action": "approve"}
                if choice.startswith("a")
                else {"action": "revise", "note": input("note: ")}
            )
            res = resume_tailor(job.id, res["thread_id"], decision, deps, saver)
        if res.get("error"):
            sys.exit(f"stopped: {res['error']}")
        print("rendered:", *res.get("pdfs", []), sep="\n  ")
    finally:
        if closer:
            closer.__exit__(None, None, None)


if __name__ == "__main__":
    main()
