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
    args = ap.parse_args(argv)

    if args.group == "db":
        _migrate()
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


if __name__ == "__main__":
    main()
