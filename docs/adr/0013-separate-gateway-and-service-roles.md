# ADR 0013: A separate gateway service and one database role per service

- Status: Accepted
- Date: 2026-10-11

## Context
The Phase 7 security review found that the gateway ran inside the worker process, which also
handles untrusted content, and that every service shared one database credential while the
human-only guards trusted a self-asserted `app.actor`. A compromised worker could have read all
channel tokens or forged the human decisions.

## Decision
- **Gateway service** (`ytcore/gateway/{server,main,auth,remote}.py`): `POST /v1/publish
  {job_id, token}` is its only route. The worker reaches it through `RemoteGateway`, with the
  same `publish()` interface as the in-process service and with each refusal keeping its type
  (PublishRefused, QuotaExhausted ...). Callers are authenticated by Cloud Run IAM and a
  Google ID token (audience `gateway-<env>`, allowed caller list). Only the gateway's service
  account reads channel OAuth tokens. In production the worker refuses to start without an
  https gateway URL (settings validator), never builds a YouTube client, and accepts only an
  approval the hub already recorded (`hub_records_approvals`). Dev and tests keep the
  in-process service.
- **Database roles** (migration 014): `yt_hub`, `yt_worker`, `yt_gateway`, `yt_ops`. The hub
  alone can record approvals, approve knowledge, change kill switches, sign off rollouts and
  confirm tier promotions; the gateway alone writes uploads and the quota ledger; the worker
  produces; ops jobs plan, monitor, pause (status column only), run analytics and evaluate
  tiers. Trigger helpers that write on behalf of callers (`audit`, job events, costs, quota,
  tier history) are SECURITY DEFINER so callers need no direct rights on those tables.
  Each service has its own login and its own connection-string secret; `ytctl ops grant-role`
  and the preflight check enforce one role per login.
- **Tests** run each service with exactly its role (`options=-c role=...`): the 20-channel
  week, the hub workflow and the hub API tests pass that way, and negative tests show that a
  worker, gateway or ops connection cannot approve, sign off, release a switch, promote a tier
  or read sessions, even after setting `app.actor = 'hub:alice'`.

## Consequences
- Production needs four database logins and the role grants (go-live runbook section 0).
- The ops role can pause clusters and channels but not resume them or change anything else.
- Any role can still insert audit events through `audit()`.
