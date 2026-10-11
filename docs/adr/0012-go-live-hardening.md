# ADR 0012: Go-live hardening

- Status: Accepted
- Date: 2026-10-11

## Decision
- **Supervisor continue-as-new**: a cluster supervisor continues as new when its history is long
  (`max_history_events`, default 4000) and no job is in production, carrying its queue, counters
  and the last 5000 job ids (duplicate-signal protection). Children are abandoned, as before;
  their `produced` signals reach the next run by workflow id. Tested with a tiny history limit.
- **Preflight** (`ytctl ops preflight`, `ytcore/ops/preflight.py`): a read-only go/no-go report
  (migrations, audit chain, kill switches, per-channel readiness incl. resolvable credentials
  and approved knowledge, stuck jobs, pending uploads). It never writes and never prints a
  secret. Exit code 1 on any FAIL.
- **Go-live runbook** with a game day: the system goes live with the global kill switch engaged,
  one channel, one video, and the stop conditions written down.

## Not done / accepted
- Strikes are still entered by a person; YouTube offers no API for them.
- Sign-off timestamps and `live_since` can be backdated only with direct database privileges;
  the application never does it and the rollout tests rely on it.
