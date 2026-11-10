# Dev shortcuts. CI calls the same targets so local and CI never drift.
UV ?= $(shell command -v /opt/homebrew/bin/uv || command -v uv)
export PATH := /Applications/Docker.app/Contents/Resources/bin:$(PATH)

.PHONY: install lint test check db-up migrate index eval eval-record eval-replay web up down

install:
	$(UV) sync

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy src tests

test:
	$(UV) run pytest -q

check: lint test

db-up:
	docker compose up -d --wait db

migrate:
	$(UV) run jobfit db migrate

index:
	$(UV) run jobfit profile index

# Evals replay recorded LLM calls (evals/cache/) and only call the provider on a cache miss.
eval:
	$(UV) run jobfit eval

# Re-record every LLM call (after changing prompts or models). Needs an API key.
eval-record:
	$(UV) run jobfit eval --mode record

# CI without keys: replay only; a missing recording fails the run.
eval-replay:
	$(UV) run jobfit eval --mode replay

web:
	cd web && npm install --no-audit --no-fund && npm run build

# Full stack in Docker: app + local Postgres/pgvector at http://localhost:8000
up:
	docker compose up --build -d

down:
	docker compose down
