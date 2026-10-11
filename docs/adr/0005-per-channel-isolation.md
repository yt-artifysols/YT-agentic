# ADR 0005: Per-channel isolation

- Status: Accepted
- Date: 2026-10-10

## Context
Up to 20 channels share agent pools. A fault, a prompt injection or a policy strike on one
channel must not spill into another.

## Decision
Each channel has its own: memory namespace and storage prefix (unique in the `channels` table),
policy and knowledge base, OAuth refresh token (one secret per channel), and budget/quota. Jobs
and approvals carry `channel_id`, and the approval foreign key `(job_id, channel_id)` makes
cross-channel approvals impossible.

## Consequences
- Agents receive a channel-scoped context; shared pools must never mix namespaces.
- Kill switches and caps can be applied per channel.
- Storage IAM conditions per prefix are added when the storage service lands.
