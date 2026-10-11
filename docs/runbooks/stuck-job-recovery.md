# Stuck jobs

**Detect**: `job.stuck` alert from the ops monitor (every 10 minutes), or `ytctl ops stuck`.
A job is stuck when it sits in an automated state longer than the monitor's limits (45 min for
production stages, 20 min approved/scheduled, 150 min publishing, 60 min published) or when
`awaiting_approval` is 20+ minutes past its deadline (the workflow should have cancelled it).

## Triage
1. `SELECT state, actor, at, payload FROM job_events WHERE job_id = '<id>' ORDER BY id;`
2. Look at the workflow in Temporal (workflow id `job-<job_id>`): pending activity, last
   failure, retry count.
3. Is a worker alive? Cloud Run worker pool `render-worker-<env>` instances > 0, recent logs.

## By cause
| Symptom | Likely cause | Action |
|---|---|---|
| Activity retrying, worker pool at 0 | no workers | scale the pool; the job resumes by itself (heartbeat timeout detects dead workers within 2 min) |
| `failed` with `BudgetExceeded` | job or daily budget | raise the budget deliberately (not to make it pass) or let the planner re-plan tomorrow |
| `failed` with `QA rework limit reached` | content keeps failing QA | read the QA flags in the hub; fix knowledge/policy, then a new job |
| `awaiting_approval` past deadline | workflow lost | start a re-plan: signal `kill` with a reason, then `plan-once` |
| `scheduled`/`publishing`, upload `pending` | crash during upload | **[Pending uploads](#pending-uploads)** below |
| `cancelled` + `replan_requested` for days | channel paused or plan exhausted | resume the channel / read the `plan.exhausted` alert |

Never edit job rows by hand: the state machine and audit chain are database-enforced and a
workaround leaves the audit trail wrong. Cancel and re-plan instead.

## Pending uploads
A worker died after YouTube may have accepted the video but before we recorded it. Retrying is
refused on purpose (it could create a duplicate).
1. `ytctl ops pending-uploads`
2. Check **YouTube Studio** for that channel: is there a video whose title/time matches?
3. Exists -> `ytctl ops adopt-upload --upload <id> --video-id <youtube id>` (job -> published; the
   next publish attempt returns it as a duplicate; live verification follows).
4. Does not exist -> `ytctl ops release-upload --upload <id> --reason "<what you checked>"`
   (job -> failed). The planner / owner creates a fresh job; it needs a fresh approval.
5. Unsure -> do neither. A pending upload costs nothing; a duplicate public video does.

## Verify
`ytctl ops stuck` returns no findings for the job; `audit_events` shows `operator:<you>`.
