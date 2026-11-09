# Job Fit Agent

Daily job ingestion, RAG fit scoring against Tylor's real experience, and verified resume/cover
letter tailoring behind a human approval step. Portfolio project and real job-search tool.

## Stack
Python 3.12 (uv), FastAPI, LangGraph, Pydantic v2, Postgres + pgvector (Supabase free tier in
prod, `docker compose` locally), fastembed (bge-small, 384-dim), Langfuse, Playwright PDFs,
Vite + React + TS frontend. Everything must stay on free tiers.

## Commands
- `make install` / `make check` (ruff, ruff format --check, mypy strict, pytest)
- `make db-up && make migrate && make index` local pgvector at localhost:54329
  (`DATABASE_URL=postgresql://postgres:jobfit@localhost:54329/jobfit`)
- `uv run jobfit profile validate|index|search "query"`
- On this Mac use `/opt/homebrew/bin/uv` (the one in ~/.local/bin is x86_64 and won't run).

## Layout
- `profile/master_profile.yaml` evidence base, every fact has an ID. Source: `reference/Tylor-C-Resume1102.pdf`.
- `profile/constraints.yaml` hard rules, enforced by `jobfit.profile.constraints.check_text`.
- `reference/layout/` PDF layout references only. `reference/voice/` tone examples only. Neither is evidence.
- `src/jobfit/rag/` embedder + vector store (pgvector and in-memory, same interface).
- `db/migrations/` idempotent SQL, applied in filename order by `jobfit db migrate`.

## Hard rules
- Never add a fact to the profile that isn't true. Generated text cites evidence IDs or it's rejected.
- No Terraform/Ansible/Puppet/Kubernetes/Jenkins/Chef. Docker and GitHub Actions only after this
  project ships them (see `pending_tools` in constraints.yaml).
- BAS in IT is "In progress". No bachelor's degree claims.
- No em dashes in generated text. No banned phrases (constraints.yaml).
- No LinkedIn/Indeed scraping. Only public job-board APIs.
- Model names and secrets come from `jobfit.config.Settings`, never hardcoded.
- Unit tests and CI run offline: use `HashEmbedder` / `InMemoryStore`, mark DB tests `@pytest.mark.db`.
