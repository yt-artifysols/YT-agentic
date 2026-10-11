# ADR 0010: Scale to 20 channels by configuration

- Status: Accepted
- Date: 2026-10-11

## Context
Pilot ran 1-3 channels. Growth to 20 channels in clusters must not need code changes, must share
limited agent/GPU capacity fairly, and must not let a new cluster go live before an earlier one
has proven itself.

## Decision
- **Fleet config** (`config/clusters|channels/*.yaml`, `ytcore/channels`): schema-validated
  (20 channel cap, quota arithmetic per Google project, rollout order, no credential-shaped
  strings) and applied idempotently (`ytctl channel apply`). Brand and policy become knowledge
  drafts; humans approve. `ytctl channel onboard <slug>` prints the checklist and OAuth steps;
  secrets are only ever vault references.
- **Cluster supervisors** (`ytcore/workflow/supervisor.py`, one Temporal workflow per cluster):
  jobs are signal-started through the supervisor, which caps jobs in production per cluster.
  A job frees its slot when it reaches `awaiting_approval` (humans must never hold capacity).
- **Fair-share pools** (migration 011, `ytcore/scheduling`): DB-backed leases with global and
  per-cluster limits per pool (research, script_seo, media, qa, gpu_render); waiters are served
  by nearest publish deadline.
- **Slot allocator** (`ytcore/planner/daily.py`): inside the publish window, deterministic
  per-channel jitter, minimum spacing in a cluster, no shared minute across the fleet.
- **Hub at scale**: queue sort/limit/offset and cluster filter, batch approve (each item still
  needs its own content-hash-bound token), cluster and scorecard pages.
- **Rollout gate** (migration 011, enforced by triggers, error YTR01): a cluster with
  `requires_cluster` cannot be enabled until the prerequisite has been live `gate_days` AND an
  admin signed it off after that period. Channels of a dark cluster cannot publish
  (`channel_publishable`). Enabling is a hub action; the global kill switch still wins.

## Consequences
- The 2-week live gate cannot be simulated; the DB rule is tested with backdated timestamps.
- Load test (`make scale-sim`): 20 channels x 7 days with injected provider errors and rate
  limits, fake providers; asserts success rate, cost per video, approval queue depth, pool and
  cluster concurrency, upload count == live jobs, and audit chain integrity.
- Simulated days share one real quota day, so sim Google projects get a large quota; real quota
  arithmetic is validated per day by the config schema.
