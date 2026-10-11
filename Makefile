# Developer entry points. CI runs the same targets.
PY ?= .venv/bin/python
COMPOSE ?= docker compose -f infra/docker-compose.yml

# Local Postgres from `make up` (dev-only credentials, see infra/docker-compose.yml).
export PGHOST ?= localhost
export PGPORT ?= 5432
export PGUSER ?= ytcore
export PGPASSWORD ?= ytcore-dev

.PHONY: lock scale-sim setup lint format typecheck test test-db test-integration test-all check-generated check-migrations \
        audit secrets-scan up down tf-validate site ui-check test-e2e hub evals redteam restore-drill check-observability

setup:
	uv venv .venv
	uv pip install -p .venv --require-hashes -r requirements.lock
	uv pip install -p .venv --no-deps -e .
	$(PY) -m pre_commit install

# Re-resolve and re-hash every dependency (review the diff, then commit requirements.lock).
lock:
	uv pip compile pyproject.toml --extra dev --extra hub --extra youtube --extra agents --extra gcp --extra gateway --extra otel --generate-hashes --python-version 3.12 -o requirements.lock
	uv pip compile pyproject.toml --extra hub --extra youtube --extra agents --extra gcp --extra gateway --extra otel --generate-hashes --python-version 3.12 -o requirements-runtime.lock
	uv pip compile pyproject.toml --extra hub --generate-hashes --python-version 3.12 -o deploy/vercel/hub-api/requirements.txt
	cp deploy/vercel/hub-api/requirements.txt deploy/vercel/jobs/requirements.txt

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

# Pipeline + gateway against a real Postgres (throwaway database per run).
test-integration:
	REQUIRE_DB=1 $(PY) -m pytest -q -m integration

# 20 channels x 7 simulated days with injected provider faults (writes docs/reports/scale-sim.json).
scale-sim:
	mkdir -p docs/reports
	SCALE_REPORT=docs/reports/scale-sim.json REQUIRE_DB=1 $(PY) -m pytest -q tests/integration/test_scale_week.py

# Agent regression evals vs ytcore/evals/baselines (fails on lower scores or unrecorded
# prompt/model changes).
evals:
	$(PY) -m ytcore.evals run --agent all

# Adversarial suite: forged/replayed/edited approvals, direct gateway calls, tool and injection
# attacks, secrets in logs and prompts, hostile assets.
redteam:
	$(PY) -m pytest -q tests/redteam

# Backup, restore into a scratch database/store and verify against the manifests.
restore-drill:
	PYTHON=$(PY) scripts/restore_drill.sh

check-observability:
	$(PY) -m ytcore.observability.dashboards --check

test-all: lint typecheck check-generated check-observability check-migrations test evals test-db test-integration restore-drill

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

# Static development-status page (deployed by Vercel from site/, see vercel.json).
site:
	$(PY) scripts/build_site.py

# Approval hub UI (Next.js): install, typecheck, build, dependency audit.
ui-check:
	cd ui/hub && npm ci --no-fund && npx tsc --noEmit && NEXT_TELEMETRY_DISABLED=1 npx next build && npm audit --omit=dev

# Full UI end-to-end run (Postgres from PG*, Temporal dev server, worker, hub API, Playwright
# with a virtual passkey) followed by database verification.
test-e2e:
	PYTHON=$(abspath $(PY)) ui/hub/e2e/run.sh

# Run the hub API locally (dev key from `python -m ytcore.approvals.service.cli gen-dev-key`).
hub:
	$(PY) -m uvicorn ytcore.approvals.service.main:app --port 8001
