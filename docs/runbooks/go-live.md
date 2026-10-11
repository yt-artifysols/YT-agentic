# Go-live checklist (first real channels, then each next cluster)

Nothing publishes without your approval, and every step below is reversible with the kill
switch ([kill-switch.md](kill-switch.md)). Do the steps in order; stop at the first failure.

## 0. Blocking prerequisites (found by the Phase 7 security review; NOT built yet)
Do not go live with real credentials until each of these is closed (ADR 0012):
- [x] **Separate gateway service** (ADR 0013): `ytcore.gateway.main` is its own Cloud Run
      service; only the worker's service account may invoke it; workers hold no YouTube
      credentials. Verify with a game-day publish.
- [x] **Per-service database roles** (migration 014). Setup per environment, as the database
      admin: create four logins (Cloud SQL IAM users, one per service account), then
      `ytctl ops grant-role --role yt_hub --login <hub login>` (and `yt_worker`, `yt_gateway`,
      `yt_ops`, `yt_backup`). Store each login's connection string only in its own `database-url-<service>`
      secret. `ytctl ops preflight` fails if one login holds two roles or is a superuser.
- [x] Supply chain: hash-locked dependencies (`make lock`), actions and hooks pinned by SHA,
      image digests enforced in Terraform, Dockerfile installs only from the lock.
- [x] SSRF: the web fetcher resolves once and connects to that address; total deadline.
- [x] Storage: the worker can create and read assets only (no delete, no overwrite).
- [ ] Render workers sandboxed with restricted egress before real media providers are enabled.
Closed in Phase 7: Cloud KMS signing provider and Terraform, hub production settings and asset
source, auth rate limiter, explicit "I reviewed this version" step before approve.

## 1. Before anything is real (once)
- [ ] Production Terraform applied (`make tf-validate`, then plan/apply by a person); signing key
      is KMS-backed and readable by the hub's service account only; the gateway gets the PUBLIC
      key only ([key-rotation.md](key-rotation.md)).
- [ ] `ENV=production` workers start (they refuse fakes, dev secrets and a non-`prod` Temporal
      namespace). Run `python -m ytcore.db.migrate` against the production database.
- [ ] Backups: run `make restore-drill` against a copy and read the result
      ([backup-restore.md](backup-restore.md)). Note the date.
- [ ] Alerts reach a human: trigger a test alert and confirm it arrives. Name the on-call person.
- [ ] Hub: register admins and approvers with passkeys; at least two admins.
- [ ] Set the global kill switch ENGAGED. Go live with it engaged; release it only in step 5.

## 2. Per channel (repeat)
- [ ] Config PR merged, `ytctl channel apply`, `ytctl channel onboard <slug>` checklist all done
      ([cluster-rollout.md](cluster-rollout.md)).
- [ ] OAuth linked by the channel owner (upload scope, then the separate read-only analytics
      scope); refresh tokens only in Secret Manager.
- [ ] Brand, policy and banned-claims knowledge approved in the hub.
- [ ] Shadow dry run reviewed (`ytctl channel onboard <slug> --dry-run-jobs 3`), then
      `autonomy_tier: gated` through a reviewed PR. New channels stay on approve-all (tier 1).

## 3. Preflight (read-only, safe any time)
`ytctl ops preflight` must exit 0 (add `--check-secrets` only when running as the gateway
identity: it resolves each channel's upload credential without printing it): migrations applied, audit chain intact, every gated channel
has credentials that resolve, an approved brand/policy/banned-claims set, a budget and an
enabled cluster, no stuck jobs or alerts, no uploads left pending. Warnings (kill switch
engaged, scoped pauses) are expected at this point.

## 4. Game day (with the kill switch engaged, a staging copy preferred)
- [ ] Try to approve a job: the hub refuses while the switch is engaged.
- [ ] Release for ONE channel only (pause the others by channel switch), approve ONE video,
      watch it upload (`privacy_status` defaults to private; keep the first videos private) and
      check it in YouTube Studio.
- [ ] Engage the global switch mid-run: the next approval and the gateway both refuse.
- [ ] Stop a worker mid-render: the job resumes; nothing uploads twice
      ([stuck-job-recovery.md](stuck-job-recovery.md)).

## 5. Release
Release the global switch. For the first two weeks of a cluster: approve everything yourself,
read the Tiers and Scorecards pages daily, record any strike with `ytctl tier strike`. Cluster B
stays dark until cluster A has run live for its gate period and an admin signs it off.

## Stop conditions (engage the kill switch first, investigate second)
A strike or warning, a video that should not have passed QA, an unexpected upload, quota or
budget alarms, the audit chain check failing, or anything you cannot explain.
