# Phase 2 plan: knowledge base, owner inputs and agents v1 (single channel, shadow mode)

## Files
- `ytcore/db/migrations/006_knowledge_inputs.sql`: `knowledge_docs` (versioned per channel and
  kind; drafts → approved/rejected only by an active human `approver` hub user; approved rows
  immutable; append-only), `job_knowledge` (versions pinned per job), `owner_inputs`
  (idea/footage/note/goal, sha256, new → used → archived), `job_owner_inputs` (provenance).
  Composite foreign keys `(…, channel_id)` make cross-channel links impossible.
- `ytcore/db/migrations/007_agents_platform.sql`: pgvector `channel_memory` (FK to the channel's
  own namespace), `rights_register`, versioned `job_assets` (sha256, license_ref required for
  music/stock), per-job budget (`channels.job_budget_cents`, SQLSTATE YTB02), cost provider
  detail, `alerts`.
- `ytcore/knowledge`, `ytcore/inputs`: models, Postgres and in-memory repositories bound to one
  channel, line diff. Approval of knowledge lives only in the hub (`ytcore/approvals/service`).
- `ytcore/planner/brief.py`: brief generator (new owner inputs first, goals, playbook fallback).
- `ytcore/agents`: typed IO, provider interfaces (LLM/TTS/video/image) with fakes and an
  Anthropic LLM adapter, versioned prompts, untrusted data blocks, per-agent tool allowlists,
  cost meter with budgets and alerts, channel context loader, memory, the four agent pools,
  manifest builder, pipeline used by `JobActivities`.
- `ytcore/evals`: 20+ briefs, rubric checks, baselines, `python -m ytcore.evals run`.
- `ytcore/cli.py` (`ytctl input add|list`, `ytctl knowledge propose|show`); hub endpoints for
  knowledge drafts (list, diff, approve, reject) and owner inputs.

## Tests
SQL guards (`tests/schema_guard_knowledge_test.sql`); unit tests for injection, tool allowlists,
namespace isolation, budgets, knowledge rules, provenance; Temporal workflow tests with agents;
Postgres integration tests incl. the exit gate (10 shadow jobs to `awaiting_approval`).

## Risks
- Fake providers make agent quality untestable until real providers and keys exist.
- `app.actor` is attribution, not authentication; the knowledge approval guard therefore checks
  the approver against `hub_users` (role approver, active).
