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
- `uv run jobfit ingest [--source greenhouse] [--no-score] [--top N]` daily pipeline
- `uv run jobfit add URL` or `add --text FILE --company X --title Y` manual posting + score
- `make eval` (replay recorded LLM calls, call the API only on a miss) / `make eval-record` (re-record
  after prompt or model changes, needs a key) / `make eval-replay` (CI, no key). Gate: exit 1 on
  any hallucinated claim in final drafts, failed injection case, or errored case.
- `uv run jobfit fit posting.txt --company X --title Y [--tailor]` live run (uses .env keys)
- Dashboard: `cd web && npm install && npm run build`, then `uv run jobfit serve` (http://127.0.0.1:8000).
  UI dev with hot reload: `jobfit serve` + `cd web && npm run dev` (Vite on :5173 proxies /api).
- `uv run playwright install chromium` once, for PDF rendering and `-m render` tests
- On this Mac use `/opt/homebrew/bin/uv` (the one in ~/.local/bin is x86_64 and won't run).

## Layout
- `profile/master_profile.yaml` evidence base, every fact has an ID. Source: `reference/Tylor-C-Resume1102.pdf`.
- `profile/constraints.yaml` hard rules, enforced by `jobfit.profile.constraints.check_text`.
- `reference/layout/` PDF layout references only. `reference/voice/` tone examples only. Neither is evidence.
- `src/jobfit/rag/` embedder + vector store (pgvector and in-memory, same interface).
- `db/migrations/` idempotent SQL, applied in filename order by `jobfit db migrate`.
- `src/jobfit/llm/` provider socket (anthropic, groq, fake) + cost ledger. Tiers per node in `config/models.yaml`.
- `src/jobfit/graphs/` fit + tailor LangGraph graphs, `sanitize.py` (injection guard), `verify.py` (claim checks).
- `src/jobfit/render/` Jinja HTML/CSS + Playwright PDFs. Layout measured from `reference/layout/`.
- `config/tech_lexicon.yaml` tech names the verifier rejects unless they appear in the profile.
- `config/watchlist.yaml` company boards; `config/ingest.yaml` sources, prefilter, top N.
- `src/jobfit/ingest/` source adapters, prefilter, pipeline, manual add. `store/ingest_store.py` closing rules.
- `.github/workflows/daily-ingest.yml` 11:00 UTC cron; secrets listed in its header.
- `src/jobfit/api/` FastAPI (`app.py` routes, `queries.py` read SQL, `tasks.py` background
  scoring/tailoring, `gaps.py`). `web/` Vite + React + TS dashboard; FastAPI serves `web/dist`.
- `evals/cases/cases.yaml` + `<id>.txt` postings; `evals/cache/` recorded LLM calls (commit it);
  `evals/report.md` + `history.jsonl` committed, `report.json` gitignored. Code in `src/jobfit/evals/`.

## Hard rules
- Never add a fact to the profile that isn't true. Generated text cites evidence IDs or it's rejected.
- No Terraform/Ansible/Puppet/Kubernetes/Jenkins/Chef. Docker and GitHub Actions only after this
  project ships them (see `pending_tools` in constraints.yaml).
- BAS in IT is "In progress". No bachelor's degree claims.
- No em dashes in generated text. No banned phrases (constraints.yaml).
- No LinkedIn/Indeed scraping. Only public job-board APIs. `ingest/manual.py` refuses those hosts.
- Remotive / Remote OK terms: keep their job URL as the link and credit them as the source.
- A failed fetch never closes jobs. Auto-close only moves New/Shortlisted cards to Closed.
- Model names and secrets come from `jobfit.config.Settings`, never hardcoded.
- Unit tests and CI run offline: use `HashEmbedder` / `InMemoryStore` / `FakeProvider`, mark DB tests
  `@pytest.mark.db` and Chromium tests `@pytest.mark.render`.
- Fit score is computed in code (`graphs/fit.py:compute_score`), never taken from the model.
- Titles, companies, dates, education, skill names on the PDF come from the profile, not the LLM.
- Groq free tier is ~8k tokens/min per model: a tailor run takes 2 to 4 minutes. That's expected.
- Any prompt change invalidates the eval cache: run `make eval-record` and commit `evals/cache/`.
- UI: dark default + light toggle, teal is the only accent, no gradients/pills/emoji, lucide icons,
  fonts bundled via @fontsource (no CDN). Keep `web/src/types.ts` in sync with schemas.py by hand.
- Playwright sync API is per-thread: use `render.pdf.default_renderer()` (thread-local), never share one.
