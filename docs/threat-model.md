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
| 3 | **Prompt injection via sources** | External and owner-submitted content reaches models only inside delimited data blocks whose markers cannot be forged (`ytcore/agents/untrusted.py`); system prompts carry only trusted text; requests carry no tools and replies are parsed into strict schemas, so a model cannot call a tool; per-agent tool allowlists are enforced in code; QA flags instruction-like text and blocks it in outputs; agents have no publish or approval capability; knowledge approval requires an active human approver (DB-enforced). Tested with a deliberately gullible fake model. |
| 4 | **Malicious media** (crafted files exploiting decoders) | Media processed in sandboxed, network-restricted workers; size caps, media-type allowlists and a signature deny-list (executables, archives, scripts, markup) applied when an agent stores an asset and again by the gateway before upload (`ytcore/safety/assets.py`); content hashing; no media processing in the approval hub or gateway. |
| 5 | **Quota exhaustion** (YouTube API, LLM spend) | Per-channel budgets and caps on event-triggered jobs; gateway as a single quota choke point; alerts and kill switches; retries with backoff and caps. |
| 6 | **Insider misuse** | Named approvers recorded in tokens and audit events; hash-chained, append-only audit log with tamper detection; least-privilege IAM per service; production changes via reviewed PRs. |
| 7 | **Supply-chain compromise** | Dependency audit (pip-audit) in CI; pinned, checksum-verified tool downloads; committed Terraform lock files; secret scan; minimal CI permissions (`contents: read`). |
| 8 | **Log leakage** | A process-wide log scrubber (messages, args, tracebacks), prompt scrubbing of external content, intake rejection of credential-shaped owner inputs and knowledge, and a repo-wide secret-shape test; secrets referenced by `vault://` refs only; settings validation never echoes inputs (`hide_input_in_errors`); `SecretValue` redacts on str/repr; no prompts with secrets. |
| 9 | **Provider outage** (YouTube, LLM, cloud) | Temporal durable retries and timers; jobs pause rather than fail open; missed approval deadlines cancel and re-plan; nothing auto-publishes when a dependency recovers. |
| 10 | **Channel strike cascade** | Per-channel isolation (namespaces, tokens, budgets); per-channel and global kill switches; banned-claims policy and QA before approval; risk scoring, cross-channel duplicate detection and a cluster circuit breaker (rejection rate, policy flags, cost anomalies) that pauses a cluster and alerts operators; the global/cluster/channel kill switches are DB-enforced and re-checked by the gateway just before upload; strike signals pause the affected channel only. |

## Residual risks / follow-ups

- Approval key rotation (key id claim) is not implemented yet.
- The research web fetcher refuses non-https and non-public addresses (SSRF); DNS rebinding
  between the check and the connect is not fully closed (resolve-and-pin is a follow-up).
- Real media providers (TTS, image, video, stock) are not chosen; production refuses the fakes.
- `app.actor` is attribution, not authentication; knowledge approval therefore checks the
  approver against active `hub_users` with the approver role, but per-service DB roles are
  still needed to stop a compromised worker from writing as the hub.
- Approval hub identity is passkey-based (ADR 0006); recovery for lost passkeys is an admin
  re-invite and must stay a manual, audited step.
- Hub WebAuthn challenges and rate limits are in-process (single instance) for now.
- Phase 3: elections/political-ads policy page was not fetched; review `docs/policy-sources.md`.
- Phase 3: the fingerprint index is a shared platform table; per-service DB roles are needed so
  only QA/safety code can read it.
- Phase 3: the circuit breaker must be scheduled (cron/Temporal schedule); thresholds are defaults.
