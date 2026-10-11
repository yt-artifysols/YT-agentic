# ADR 0006: Approval hub with passkeys and server-side signing

- Status: Accepted
- Date: 2026-10-10

## Context
Every publish needs a human approval bound to the exact content. Approvals must be hard to
forge, hard to phish, and impossible to apply to content that changed after review.

## Decision
- A dedicated FastAPI service (`ytcore/approvals/service`) is the only place approval tokens
  are issued. It signs through a `KeyProvider` (dev: local key file; production: KMS, the
  private key never leaves it). Gateway, store, workflow and agents get the public key only.
- Approve re-checks the job's CURRENT content hash against the hash the reviewer saw, signs a
  token, consumes it in the DB (approvals row + `approved`, atomically) and signals the
  Temporal workflow. A stale preview gets HTTP 409 and nothing is approved.
- Edit creates a new manifest version with a new hash and sends the job back to QA; it never
  approves. Reject records a reason and a rework stage (or discard).
- Batch approval exists only for the fast lane (QA passed, risk <= threshold, no flags); each
  video gets its own token and its own hash check.
- Sign-in uses WebAuthn passkeys (py_webauthn, user verification required); roles are
  approver (decides), operator (kill switches) and admin (users). Server-side sessions with
  idle and absolute timeouts; CSRF token + same-origin check on every POST; rate limits;
  audit events for every action. The Next.js UI proxies `/api` so cookies stay first-party.

## Consequences
- WebAuthn challenges and rate-limit counters live in process: run one hub instance, or move
  them to a shared store before scaling out.
- The approval token travels in the Temporal signal and lives in workflow history; it is
  single-use and bound to one job/channel/hash, so history readers cannot reuse it elsewhere.
- KMS signing is an interface until wired; production settings refuse the local key file.
