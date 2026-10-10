# ADR 0008: Safety and compliance

- Status: Accepted
- Date: 2026-10-10

## Context
Unsafe output must be hard to produce and hard to approve by mistake, across up to 20 channels,
without weakening isolation or the single approval gate.

## Decision
- **Risk score** (`ytcore/safety/risk.py`): noisy-or of named reasons (unsourced claims,
  sensitive topics, rights, brand drift, novelty, provenance), 0..1, stored in `job_risk`.
  Escalate/block thresholds are per channel (`SafetyPolicy` in the approved `policy` doc).
  Owner input only removes the `no_owner_input` reason; it never bypasses a check.
- **Policy checks** are deterministic rules (`checks.py`); the LLM judges only borderline titles.
  Rules cite `docs/policy-sources.md`.
- **Duplicates** (`duplicates.py`): word 5-gram MinHash + embeddings, media hashes, title
  templates, thumbnail text. The fingerprint index is a platform service read only by QA; flags
  carry a generic `detail` (may reach the script writer) and a human-only `evidence`.
- **Kill switches**: global, cluster, channel. DB triggers refuse approvals, scheduling and
  publishing (YTC01); the gateway re-checks right before the upload call.
- **Circuit breaker**: rejection rate, flags per video and cost anomalies pause a cluster and
  raise an operator alert. It only pauses; resuming is a human decision. Window restarts on trip.
- **Secrets**: one pattern list (`scrub.py`) drives the log scrubber, prompt scrubber, intake
  rejection (owner inputs, knowledge) and a repo scan test.
- **Assets**: size caps, media-type allowlists and a signature deny-list at storage and at the
  gateway.

## Consequences
Thresholds and rules need human review (see the phase report). The breaker needs a scheduler
(`python -m ytcore.safety.breaker run-once`).
