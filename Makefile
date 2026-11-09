# Dev shortcuts. CI calls the same targets so local and CI never drift.
UV ?= $(shell command -v /opt/homebrew/bin/uv || command -v uv)
export PATH := /Applications/Docker.app/Contents/Resources/bin:$(PATH)

.PHONY: install lint test check db-up migrate index

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
