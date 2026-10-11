# 0014. Staging on Vercel: database-driven driver instead of Temporal

Status: accepted (staging only)

## Context
The owner hosts on Vercel. Functions are short-lived and stateless: no long-running worker, no
Temporal, no KMS, no process memory shared between requests.

## Decision
- **Hub API** and **job driver** are separate Python projects with separate database logins
  (`yt_hub`, `yt_worker`/`yt_ops`). The hub holds the private key (encrypted env var, staging
  only); the driver holds the public key only.
- State that lived in memory or Temporal moves to Postgres (migration 015): WebAuthn ceremonies
  and step-up tickets (single-use `DELETE ... RETURNING`), a decision mailbox from hub to driver,
  driver leases and artifacts, and assets (<= 5 MiB).
- The driver (`ytcore/serverless/driver.py`) follows `VideoJobWorkflow` step by step and reuses
  the same activities, so every rule stays where it was: the DB triggers, the gateway's token
  check, and "approve only accepts an approval the hub already recorded". A decision is consumed
  only after the job has acted on it; the stored token is erased by trigger on consumption.
- Cron and a hub poke after each decision drive the jobs. The poke carries no data.

## Consequences
- Real publishing is not enabled here: the YouTube client is the fake one, and the runtime
  refuses `real` and `ENV=production`. Production keeps Temporal, KMS and the gateway (ADR 0013).
- No render sandbox; rendering stays on fakes. Rate limiting is per instance until the platform
  firewall is configured.
- A stolen DB read could expose an unconsumed approval token for already-approved content; it is
  bound to job, channel and content hash, expires, and is erased when used.
