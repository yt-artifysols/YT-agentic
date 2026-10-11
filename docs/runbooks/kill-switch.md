# Kill switches

Three levels, all enforced by the database (approvals, scheduling and uploads are refused) and
re-checked by the gateway immediately before every upload call:

| Level | Use when | Hub action (operator or admin) |
|---|---|---|
| Channel | one channel has a problem (strike, bad output, wrong credentials) | Controls -> channel -> Pause (`POST /api/controls/channels/{id}`) |
| Cluster | a group of channels shares the cause (same niche, same project, breaker trip) | Controls -> cluster -> Pause (`POST /api/controls/clusters/{id}`) |
| Global | anything you do not understand yet, a leaked key, a platform incident | Controls -> Global -> Pause (`POST /api/controls/global`) |

Every pause needs a reason; it is audited and operators are notified.

## Contain
1. Pause at the **smallest level that covers the problem**. If unsure, pause globally: pausing
   only stops publishing and approvals, nothing is lost. Jobs keep being produced up to
   `awaiting_approval`; the planner skips paused channels.
2. Confirm: the hub shows the switch engaged; `ytctl ops plans` shows no new plans for the
   channel; trying to approve returns 423 (locked).

## What happens to in-flight work
- `scheduled`/`publishing` jobs: the gateway refuses (`kill switch engaged`) and, if it was
  already mid-publish, fails the upload before calling YouTube. Re-plan the job once resumed.
- `awaiting_approval` jobs: wait; the workflow cancels and re-plans them at their deadline.
- The **cluster circuit breaker** pauses clusters by itself (rejection rate, policy flags, cost
  anomaly) and alerts; it never resumes anything.

## Resume
Only after the cause is fixed and written down: Controls -> Resume (same endpoint, `paused:
false`). Resuming a cluster that the breaker tripped restarts its measurement window.
Then run `python -m ytcore.workflow.worker plan-once` (or wait for the hourly job) and watch
the first publish.

## Verify
`SELECT * FROM audit_events WHERE action LIKE 'killswitch.%' ORDER BY id DESC LIMIT 20;`
