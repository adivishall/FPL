# Task runner. `make help` lists targets. All Python runs through uv (locked environment).
.DEFAULT_GOAL := help
UV ?= uv
PY := $(UV) run

.PHONY: help install lint fmt typecheck layering test test-unit test-integration test-slow ci \
        schemas migrate services-up services-down docker-build

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Create the locked virtualenv (all workspace packages + dev tools)
	$(UV) sync --frozen

lint: ## Ruff lint + format check
	$(PY) ruff check .
	$(PY) ruff format --check .

fmt: ## Auto-format and fix lint
	$(PY) ruff check . --fix
	$(PY) ruff format .

typecheck: ## mypy over all first-party packages
	$(PY) mypy packages services apps/api/src apps/worker/src

layering: ## Enforce architecture boundaries (ADR-0002)
	$(PY) lint-imports

test: ## Fast test suite (unit, property, integration with ephemeral Postgres)
	$(PY) pytest -q -m "not slow and not network"

test-unit: ## Unit + property tests only (no infrastructure)
	$(PY) pytest -q tests/unit

test-integration: ## Integration tests (real PostgreSQL/Redis)
	$(PY) pytest -q -m integration

test-slow: ## Slow tests (real-data downloads, training, long optimisations)
	$(PY) pytest -q -m "slow"

ci: lint typecheck layering test ## Everything CI runs locally

schemas: ## Regenerate generated schemas (ruleset JSON-schema)
	$(PY) python -m fpl_domain.rules.schema

migrate: ## Apply DB migrations to $$FPL_DATABASE_URL
	$(PY) alembic upgrade head

services-up: ## Start PostgreSQL + Redis via docker compose
	docker compose up -d postgres redis

services-down: ## Stop local services
	docker compose down

docker-build: ## Build the API/worker image
	docker build -f infra/docker/python.Dockerfile -t fpl-engine:local .
