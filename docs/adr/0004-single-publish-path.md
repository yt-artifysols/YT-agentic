# ADR 0004: Single publish path through the YouTube gateway

- Status: Accepted
- Date: 2026-10-10

## Context
Many agents and services exist; any of them talking to the YouTube upload API would bypass
the approval gate.

## Decision
The YouTube gateway is the only component with YouTube OAuth credentials and the only one that
calls the upload API. It verifies the approval token before every upload. Triggered jobs,
experiments and derived jobs (Shorts, clips) use the same workflow and the same gate. Nothing is
ever auto-published.

## Consequences
- IAM grants channel refresh tokens to the gateway service account only (see Terraform).
- Production config refuses the fake YouTube client; dev config refuses the real one.
- The gateway is a choke point for quota management and kill switches.
