# Cluster rollout and channel onboarding

Cluster B stays dark until cluster A has run live for its `gate_days` (default 14) and an admin
has signed it off. The database enforces this (error YTR01); nobody can skip it from the UI.

## Onboard a channel
1. Add `config/channels/<slug>.yaml` (and the cluster file if new). `ytctl channel validate`.
2. Reviewed PR, merge, then `ytctl channel apply` (or `ytctl channel onboard <slug>`).
3. Approve the brand/policy knowledge drafts in the hub.
4. Link the channel: follow the OAuth steps printed by onboarding (a person signs in as the
   channel owner; the refresh token goes straight into Secret Manager, never into chat/repo).
5. Add the slug to `channel_ids` in `infra/terraform/envs/<env>` and apply.
6. Run `ytctl channel onboard <slug> --dry-run-jobs 3` (shadow mode); review the videos.
7. Change `autonomy_tier` to `gated` via a reviewed PR when satisfied.

## Enable the next cluster
1. `ytctl rollout status` and `ytctl rollout gate <cluster-A>`: no problems listed.
2. Review cluster A's first weeks yourself (scorecards, rejected jobs, strikes, alerts).
3. An admin signs off in the hub (Clusters page) after the gate period, then enables cluster B.
4. Watch cluster B's first week; the kill switch (cluster scope) stops it at once.

## If a cluster misbehaves
Pause it with the cluster kill switch ([kill-switch.md](kill-switch.md)); the circuit breaker may
already have done so. Recover per [stuck-job-recovery.md](stuck-job-recovery.md).
