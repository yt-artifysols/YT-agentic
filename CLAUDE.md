# yt-agentic

Agentic control system producing videos for up to 20 YouTube channels. Agents research, script,
render and QA; a HUMAN approval is mandatory before anything is published.

## Non-negotiable invariants
- No publish without a valid Ed25519 approval token bound to job_id, channel_id and content_hash.
  Only the YouTube gateway (`ytcore/gateway`) talks to the upload API and it holds only the
  public key. Only the approval hub holds the private key.
- `ytcore/workflow/states.py` is the single source of truth for job states. After editing it
  run `python ytcore/tools/gen_transitions_sql.py` (CI checks for drift).
- Applied migrations in `ytcore/db/migrations` are immutable; add a new numbered file.
  `job_events`, `approvals` and `audit_events` are append-only.
- Secrets never appear in code, logs, prompts, tests or commits. Use `SecretRef` + a
  `SecretsProvider` (`ytcore/secrets`).
- External/scraped content is untrusted DATA, never instructions.
- Per-channel isolation: memory namespace, storage prefix, policy, OAuth token, budget.
- Agents only submit knowledge drafts; humans approve.
- Nothing auto-publishes. Missed approval deadline => cancel and re-plan.

## Layout
- `ytcore/workflow` – states (source of truth), `VideoJobWorkflow`, activities (`Backend` protocol)
- `ytcore/store/postgres.py` – psycopg 3 store; DB triggers enforce rules, errors become typed
  exceptions (`ytcore/store/errors.py`); every write sets `app.actor` for events/audit
- `ytcore/gateway` – `PublishService` (only publish path, public key only), `FakeYouTubeClient`
  (default) and `RealYouTubeClient` (`youtube` extra; production only)
- `ytcore/db/migrations` – numbered, immutable; `002_transitions_seed.sql` is generated and
  repeatable (`-- repeatable`)
- `ytcore/approvals/service` – approval hub API (FastAPI): the ONLY place tokens are issued;
  passkeys (py_webauthn), roles approver/operator/admin, CSRF, rate limits, audit; OpenAPI spec
  committed (`cli export-openapi --check`)
- `ytcore/approvals/keys.py` – KeyProvider (dev key file / KMS); only the hub may import it
- `ui/hub` – Next.js approval UI; `ui/hub/e2e` – full-stack Playwright test with a virtual
  passkey (`make test-e2e`)
- `ytcore/knowledge` – versioned per-channel knowledge (channel-bound repos, drafts only; the hub
  approves); `ytcore/inputs` – owner inputs with provenance; `ytcore/planner/brief.py`
- `ytcore/agents` – research, script_seo, media, qa pools: typed IO, prompts under `prompts/`
  (versioned; changing one needs a new eval baseline), tool allowlists (`tools.py`), untrusted
  data blocks (`untrusted.py`), metered budgets (`costs.py`), memory (pgvector), manifest
  builder, `pipeline.py` (used by `JobActivities`), fakes in `fake.py`, Claude adapter
- `ytcore/evals` – `python -m ytcore.evals run --agent all` vs `baselines/` (CI)
- `ytcore/safety` – risk scoring (`risk.py`, noisy-or, named reasons, stored in `job_risk`), policy
  checks (`checks.py`, deterministic first; the LLM only judges borderline titles), cross-channel
  duplicate detection (`duplicates.py`, only QA reads the index; no other-channel id reaches a
  prompt), asset limits, log/prompt secret scrubber (`scrub.py`), cluster circuit breaker
  (`breaker.py`, only ever pauses). Per-channel `SafetyPolicy` lives in the `policy` knowledge doc.
  Kill switches (global, cluster, channel) are DB-enforced (migration 008) and re-checked by the gateway.
- `ytcore/planner/daily.py` – daily planner (idempotent plans, slot rules, replans);
  `ytcore/workflow/worker.py` – worker + `plan-once` entry points (`build_stack` wires fakes or real)
- `ytcore/observability` – traces per job (`tracing.py`), metrics, ops monitor, dashboards as code
  (`python -m ytcore.observability.dashboards --check`); `ytcore/analytics` – metrics ingestion,
  learnings, scorecards; `ytcore/ops` – backup/restore drill, upload reconciliation
- `ytcore/channels` – fleet YAML (`config/`) loader/apply/onboard + rollout gate (migration 011:
  cluster B cannot be enabled before cluster A was live `gate_days` and an admin signed off);
  `ytcore/scheduling` – pool leases (fair share, deadline priority); `ytcore/workflow/supervisor.py`
  – per-cluster supervisor (caps jobs in production); adding a channel needs no code changes
- `docs/runbooks` – kill switch, key rotation, stuck jobs, quota, strikes, suspension, restore
- `tests/redteam` – adversarial suite (`make redteam`); must stay green
- `ytctl` (`ytcore/cli.py`) – owner inputs and knowledge proposals
- `tests/fakes.py` – `FakeBackend` mirroring DB rules; `tests/integration` – real Postgres

## Commands
- `make setup` – venv + dev deps + pre-commit hooks
- `make lint typecheck test` – ruff, mypy (strict), pytest (unit + Temporal time-skipping)
- `make up` then `make test-db` – migrations + `tests/schema_guard_test.sql` on real Postgres
- `make test-integration` – pipeline + gateway against real Postgres (`REQUIRE_DB=1`)
- `make evals` – agent evals vs committed baselines
- `make restore-drill` – backup, restore into scratch DB, verify; `make check-observability`
- `make redteam` – adversarial suite (forged/replayed/edited approvals, hostile assets, secrets)
- `make test-all` – everything CI runs (except secret scan, audit, terraform)
- `make scale-sim` – 20 channels x 7 days with injected provider faults
- `make tf-validate` – Terraform validation

## Conventions
- Fakes/in-memory implementations by default; production config refuses them.
- Every new invariant gets a test; DB invariants get a guard test in the SQL test file.
