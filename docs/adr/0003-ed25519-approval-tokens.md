# ADR 0003: Ed25519 approval tokens

- Status: Accepted
- Date: 2026-10-10

## Context
Publishing must be impossible without an explicit human approval for the exact content being
uploaded. A boolean flag in a database row can be flipped by any component with write access.

## Decision
The approval hub signs an Ed25519 token over canonical JSON claims: token id, job id, channel id,
content hash, approver, issue time and expiry (TTL at most 72h). The private key lives only in
the approval hub's secret. Verifiers (the YouTube gateway) hold only the public key and check
the signature, the job/channel/content binding and the expiry. Tokens are single use.

## Consequences
- Compromising a worker or the gateway does not allow forging approvals.
- Any change to the content after approval changes the hash and invalidates the token.
- Key rotation requires distributing a new public key; add a key id claim when rotation lands.
