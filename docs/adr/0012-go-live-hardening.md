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

## Final security review (whole repo) and what changed
Fixed: Cloud KMS signing provider (`KmsKeyProvider` + injectable client; Terraform key,
signer-only IAM for the hub, no private-key secret container, hub env incl. `YT_HUB_*`); hub
rate limiter (unauthenticated endpoints limited per client address, never per attacker-chosen
cookie; bounded memory; rightmost forwarded address); production hub refuses an in-memory asset
store and the Cloud Run hub gets the bucket source; approve needs an explicit "I reviewed this
version" confirmation and the one-key shortcut is gone; `preflight` resolves secrets only with
`--check-secrets`.

**Open blockers for real go-live** (see docs/runbooks/go-live.md section 0): gateway as its own
service, per-service database roles, supply-chain pinning, SSRF IP pinning, per-channel storage
IAM, render-worker sandbox. A step-up passkey assertion per approval is also recommended.

## Not done / accepted
- Strikes are still entered by a person; YouTube offers no API for them.
- Sign-off timestamps and `live_since` can be backdated only with direct database privileges;
  the application never does it and the rollout tests rely on it.
