# ADR 0002: Postgres plus pgvector

- Status: Accepted
- Date: 2026-10-10

## Context
We need transactional job state, approvals and an audit trail, plus semantic memory (research
notes, hook library, past scripts) per channel.

## Decision
One Postgres 16 cluster with the pgvector extension. Invariants live in the database:
- the job state machine is enforced by triggers against `job_state_transitions`, which is
  generated from `ytcore/workflow/states.py`;
- entering `approved` or `publishing` requires a non-revoked, unexpired approval bound to the
  job, its channel and its current content hash;
- `job_events`, `approvals` and `audit_events` are append-only; `audit_events` is hash-chained
  and verifiable with `audit_chain_first_break()`.
Vector memory tables are keyed by channel namespace (added in a later migration).

## Consequences
- Every DB invariant needs a guard test in `tests/schema_guard_test.sql` run against a real
  Postgres in CI.
- Applied migrations are immutable (checksum-verified by the runner, diffed against `main` in
  CI and pre-commit).
- A single engine keeps operations simple; vector workloads can move out later if needed.
