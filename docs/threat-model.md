# Threat model

Scope: the yt-agentic control system that plans, produces and publishes videos for up to 20
YouTube channels with mandatory human approval. Review this document whenever a trust boundary
changes.

## Assets

| Asset | Why it matters |
|---|---|
| YouTube OAuth refresh tokens (one per channel) | Full control of a channel |
| Approval signing key (Ed25519 private) | Can authorize any publish |
| Approved knowledge base per channel | Steers every agent: brand voice, offers, banned claims |
| Owner inputs (ideas, footage, notes) | Unpublished IP |
| Rendered media and publish packages | Content that goes public |
| Job state, approvals, audit trail | Proof of who approved what |
| LLM/API keys and budgets | Cost and quota |
| Channel standing (strikes, monetization) | Business continuity |

## Trust boundaries

1. **Operator ↔ approval hub**: the only place approvals are created; holds the private key.
2. **Orchestration/agents ↔ external content**: web pages, transcripts, comments and trends are
   untrusted DATA.
3. **Agents ↔ knowledge base**: agents may only submit drafts; humans approve changes.
4. **Workers ↔ YouTube gateway**: workers request a publish; the gateway verifies the token.
5. **Gateway ↔ YouTube API**: the only egress to the upload API; holds per-channel tokens.
6. **Services ↔ secrets manager**: per-service accounts with least-privilege access.
7. **CI/CD and dependencies ↔ runtime**: build-time code becomes runtime code.

## Top 10 threats and mitigations

| # | Threat | Mitigations |
|---|---|---|
| 1 | **Stolen OAuth refresh token** | Tokens only in the secrets manager, one per channel, readable only by the gateway service account; never in code, logs, prompts or tests (`SecretValue` masks repr; gitleaks in CI and pre-commit); revoke and rotate per channel; alerts on uploads with no matching audit event. |
| 2 | **Forged approval** | Passkey (WebAuthn) sign-in with user verification, approver role, CSRF + same-origin checks, stale-preview refusal (hash re-check); Ed25519 tokens signed only by the approval hub through a KMS-backed KeyProvider; the gateway holds only the public key; tokens bound to job, channel and content hash, expire within 72h and are single use; DB gate requires a matching non-revoked, unexpired approval row; approvals append-only. |
| 3 | **Prompt injection via sources** | External content is wrapped and passed as data, never instructions; agents have no publish capability; any output goes through QA and human approval; agents cannot edit approved knowledge. |
| 4 | **Malicious media** (crafted files exploiting decoders) | Media processed in sandboxed, network-restricted workers; type/size validation; content hashing; no media processing in the approval hub or gateway. |
| 5 | **Quota exhaustion** (YouTube API, LLM spend) | Per-channel budgets and caps on event-triggered jobs; gateway as a single quota choke point; alerts and kill switches; retries with backoff and caps. |
| 6 | **Insider misuse** | Named approvers recorded in tokens and audit events; hash-chained, append-only audit log with tamper detection; least-privilege IAM per service; production changes via reviewed PRs. |
| 7 | **Supply-chain compromise** | Dependency audit (pip-audit) in CI; pinned, checksum-verified tool downloads; committed Terraform lock files; secret scan; minimal CI permissions (`contents: read`). |
| 8 | **Log leakage** | Secrets referenced by `vault://` refs only; settings validation never echoes inputs (`hide_input_in_errors`); `SecretValue` redacts on str/repr; no prompts with secrets. |
| 9 | **Provider outage** (YouTube, LLM, cloud) | Temporal durable retries and timers; jobs pause rather than fail open; missed approval deadlines cancel and re-plan; nothing auto-publishes when a dependency recovers. |
| 10 | **Channel strike cascade** | Per-channel isolation (namespaces, tokens, budgets); per-channel and global kill switches; banned-claims policy and QA before approval; strike signals pause the affected channel only. |

## Residual risks / follow-ups

- Approval key rotation (key id claim) is not implemented yet.
- Kill switches and per-channel budgets exist as design only; implement in later phases.
- Approval hub identity is passkey-based (ADR 0006); recovery for lost passkeys is an admin
  re-invite and must stay a manual, audited step.
- Hub WebAuthn challenges and rate limits are in-process (single instance) for now.
