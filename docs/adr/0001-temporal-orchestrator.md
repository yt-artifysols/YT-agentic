# ADR 0001: Temporal as the orchestrator

- Status: Accepted
- Date: 2026-10-10

## Context
Each video job runs for hours to days: research, script, render, QA, then a human approval wait
with a deadline. The control plane needs durable timers, retries, signals (approvals, kill
switches) and a replayable history for audit, across up to 20 channels at once.

## Decision
Use Temporal workflows (`VideoJobWorkflow`) for orchestration. Agents and side effects run as
activities. Approvals and kill switches are signals. The approval deadline is a durable timer;
when it fires, the job is cancelled and re-planned, never published.

## Consequences
- Workflow code must be deterministic; all I/O goes through activities.
- Time-skipping test environments let deadline behavior be tested in milliseconds.
- Temporal is an extra stateful service (self-hosted on Postgres or Temporal Cloud).
- Postgres remains the source of truth for job state; workflow transitions go through the
  DB-enforced state machine, so a buggy workflow still cannot skip the approval gate.
