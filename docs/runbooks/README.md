# Runbooks

Who does what when something goes wrong. Each runbook says how to **detect**, **contain**,
**recover** and **verify**, and what to **write down** afterwards. All actions are audited; use
your own named account (the database records `operator:<name>`), never a shared one.

| Situation | Runbook |
|---|---|
| Rotating the approval signing key or a channel's OAuth token | [key-rotation.md](key-rotation.md) |
| Stopping publishing at once (global, cluster, channel) | [kill-switch.md](kill-switch.md) |
| A job that stopped moving, or an upload left pending | [stuck-job-recovery.md](stuck-job-recovery.md) |
| YouTube API quota exhausted or close to it | [quota-exhaustion.md](quota-exhaustion.md) |
| A strike, a policy warning, a takedown | [strike-response.md](strike-response.md) |
| A channel suspended or terminated | [channel-suspension-restore.md](channel-suspension-restore.md) |
| Onboarding a channel, enabling the next cluster | [cluster-rollout.md](cluster-rollout.md) |
| Promoting or demoting a channel's approval tier | [approval-tiers.md](approval-tiers.md) |
| Restoring Postgres or storage from backup | [backup-restore.md](backup-restore.md) |

First look when paged: `ytctl ops stuck` (jobs, budgets, quota, failed uploads),
`ytctl ops pending-uploads`, `ytctl ops plans`, and the hub's alerts page.
