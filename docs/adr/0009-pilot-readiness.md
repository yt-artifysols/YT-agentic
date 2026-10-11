# ADR 0009: Pilot readiness

- Status: Accepted
- Date: 2026-10-11

## Context
One to three channels go live with approve-all. The system must run unattended between human
approvals, show what it is doing, learn from results, and survive restarts, crashes and bad days.

## Decision
- **Daily planner** (`ytcore/planner/daily.py`, migration 010 `daily_plans`): one plan per
  channel per channel-local publish day; the job and its plan are written in one transaction
  under an advisory lock, so re-running never duplicates. Slot = the channel's local publish hour
  in its audience time zone, staggered inside a cluster by `slot_spacing_minutes`; the approval
  deadline is a lead time before the slot. Starting the workflow is idempotent (workflow id =
  job id) and unstarted plans are healed on the next run. A finished plan still occupies its day;
  only a replaced plan frees it.
- **Missed deadline** (unchanged invariant): the workflow cancels and requests a re-plan. The
  planner replaces the plan with a new attempt on the next free day, at most 4 attempts, then
  alerts an operator (`plan.exhausted`). Nothing is ever published late or automatically.
- **Decisions are buffered**, not dropped, if they arrive in the gap between the database
  reaching `awaiting_approval` and the workflow noticing (found by the week simulation); a
  decision about stale content is ignored.
- **Crash safety**: activities heartbeat; a dead worker is noticed within the heartbeat timeout
  (default 120 s) instead of the activity timeout (up to 2 h). An upload interrupted after
  YouTube accepted it stays `pending` and retries are refused; an operator adopts or releases it
  (`ytctl ops`), audited.
- **Observability**: every component derives the same trace id from the job id (no context
  propagation needed); attributes are scrubbed and carry no content. Metrics come from event
  instruments and from database snapshots (queue depth, stage latency, rework, approval time,
  cost per video, quota). Dashboards and alert rules are code and drift-checked in CI. The ops
  monitor raises stuck-job, budget, quota, gateway-failure and missing-plan alerts.
- **Analytics** use a separate read-only OAuth token per channel (never the upload token);
  results feed that channel's own memory (`performance`), once per video per ISO week, and daily
  scorecards.
- **Backups**: Cloud SQL PITR plus a daily logical dump with a manifest; the restore drill
  restores into a scratch database and verifies counts, migrations, the audit chain, safety
  triggers and every asset digest. A backup of a database whose audit chain is broken is refused.

## Consequences
Plans are keyed on channel-local days, so channel time-zone changes apply from the next plan.
Approval keys have no key id yet, so a rotation has no overlap window (see the runbook).
