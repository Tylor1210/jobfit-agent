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
    ing = sub.add_parser("ingest")
    ing.add_argument("--source", action="append", help="limit to these sources (repeatable)")
    ing.add_argument("--no-score", action="store_true", help="skip the LLM fit step")
    ing.add_argument("--top", type=int, help="override score_top_n")
    add = sub.add_parser("add")
    add.add_argument("url", nargs="?")
    add.add_argument("--text", help="file with pasted posting text")
    add.add_argument("--company")
    add.add_argument("--title")
    add.add_argument("--location")
    ev = sub.add_parser("eval")
    ev.add_argument("--mode", choices=["auto", "replay", "record"], default="auto")
    ev.add_argument("--only", action="append", help="run only these case ids (repeatable)")
    ev.add_argument("--no-tailor", action="store_true", help="skip tailor/hallucination checks")
    ev.add_argument("--save", action="store_true", help="also store the run in eval_runs")
    srv = sub.add_parser("serve")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    srv.add_argument("--reload", action="store_true")
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
    if args.group == "ingest":
        _ingest(args)
        return
    if args.group == "add":
        _add(args)
        return
    if args.group == "eval":
        _eval(args)
        return
    if args.group == "serve":
        import uvicorn

        uvicorn.run(
            "jobfit.api.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
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


def _eval(args: argparse.Namespace) -> None:
    import logging

    from jobfit.evals.cases import load_cases
    from jobfit.evals.report import save_to_db, write_reports
    from jobfit.evals.runner import run_eval
    from jobfit.rag.embedder import get_embedder

    logging.basicConfig(level=logging.WARNING)
    cases = load_cases(only=set(args.only) if args.only else None)
    results, summary = run_eval(cases, get_embedder(), mode=args.mode, tailor=not args.no_tailor)
    write_reports(results, summary)
    if args.save and get_settings().database_url:
        save_to_db(get_settings().database_url or "", results, summary)
    print((ROOT / "evals" / "report.md").read_text())
    if not summary.gate_passed:
        sys.exit(1)


def _add(args: argparse.Namespace) -> None:
    from pathlib import Path

    from jobfit.app import build_deps
    from jobfit.graphs.fit import run_fit
    from jobfit.ingest.manual import ManualAddError, from_text, from_url

    try:
        if args.text:
            if not (args.company and args.title):
                sys.exit("--text needs --company and --title")
            job = from_text(
                Path(args.text).read_text(encoding="utf-8"),
                args.company,
                args.title,
                url=args.url,
                location=args.location,
            )
        elif args.url:
            job = from_url(args.url)
        else:
            sys.exit("give a URL or --text FILE")
    except ManualAddError as e:
        sys.exit(str(e))
    deps = build_deps(render=False)
    out = run_fit(job, deps)
    if out.get("error"):
        sys.exit(f"saved nothing: {out['error']}")
    f = out["fit"]
    print(
        f"added {job.id}: {job.company} | {job.title}\n{f.fit_score}/100 {f.verdict}: "
        f"{f.verdict_line}\ngaps: {'; '.join(f.top_gaps)}"
    )


def _ingest(args: argparse.Namespace) -> None:
    import json
    import logging

    from jobfit.app import build_deps, build_ingest_store
    from jobfit.ingest.config import SourceKeys, load_ingest_config, load_watchlist
    from jobfit.ingest.pipeline import run_ingest, summary
    from jobfit.rag.embedder import get_embedder

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    deps = build_deps(render=False)
    rep = run_ingest(
        deps,
        build_ingest_store(deps),
        load_ingest_config(),
        load_watchlist(),
        SourceKeys(),
        get_embedder(),
        only=set(args.source) if args.source else None,
        score=not args.no_score,
        top_n=args.top,
    )
    print(json.dumps(summary(rep), indent=2))
    for jid, sc, verdict in rep.scored:
        job = deps.repo.get_job(jid)
        print(f"  {sc:>3} {verdict:<9} {job.company if job else ''} | {job.title if job else jid}")
    for n in rep.notes:
        print("note:", n)
    for name, st in rep.per_source.items():
        for e in st.errors:
            print(f"error [{name}] {e}")
    if rep.status == "failed":
        sys.exit(1)


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
