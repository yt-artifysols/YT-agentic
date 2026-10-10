# Developer entry points. CI runs the same targets.
PY ?= .venv/bin/python
COMPOSE ?= docker compose -f infra/docker-compose.yml

# Local Postgres from `make up` (dev-only credentials, see infra/docker-compose.yml).
export PGHOST ?= localhost
export PGPORT ?= 5432
export PGUSER ?= ytcore
export PGPASSWORD ?= ytcore-dev

.PHONY: setup lint format typecheck test test-db test-all check-generated check-migrations \
        audit secrets-scan up down tf-validate

setup:
	uv venv .venv
	uv pip install -p .venv -e '.[dev]'
	$(PY) -m pre_commit install

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format:
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

typecheck:
	$(PY) -m mypy

check-generated:
	$(PY) ytcore/tools/gen_transitions_sql.py --check

check-migrations:
	$(PY) scripts/check_migrations_immutable.py --base origin/main

test:
	$(PY) -m pytest -q

test-db:
	PYTHON=$(PY) scripts/test_db.sh

test-all: lint typecheck check-generated check-migrations test test-db

audit:
	$(PY) -m pip_audit --skip-editable

secrets-scan:
	gitleaks detect --source . --config .gitleaks.toml --redact

up:
	$(COMPOSE) up -d --wait

down:
	$(COMPOSE) down

tf-validate:
	cd infra/terraform/envs/staging && terraform init -backend=false -input=false && terraform validate
	cd infra/terraform/envs/production && terraform init -backend=false -input=false && terraform validate
