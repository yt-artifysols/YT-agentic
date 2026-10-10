# Phase 3 plan: safety and compliance

## Files
- `ytcore/safety/`: `risk.py` (0-1 noisy-or score with named reasons), `checks.py` (deterministic
  policy rules), `lexicons.py`, `duplicates.py` (cross-channel shingling + embeddings, media
  hashes, title/thumbnail templates), `breaker.py` (cluster circuit breaker), `scrub.py` (log and
  prompt secret scrubber), `assets.py` (size/type/sniff limits), `policy.py` (per-channel safety
  policy, part of the channel `policy` knowledge doc).
- Migration `008_safety.sql`: `job_risk`, `content_fingerprints` (append-only), cluster breaker
  config and trips, approvals refused while any kill switch is engaged, `qa_reports` unchanged.
- QA agent: deterministic rules first, one LLM judgment skill (`qa.v2` prompt, new baseline).
- Gateway: `PublishContext` carries the global pause; re-checked before the YouTube call.
- `tests/redteam/` plus guard tests in `tests/schema_guard_safety_test.sql`.
- `docs/policy-sources.md`, ADR 0008, threat model update.

## Risks
- Lexicon rules produce false positives/negatives; thresholds are config and need human review.
- Cross-channel detection reads other channels' fingerprints by design: only a platform service
  (never an agent or an LLM prompt) may see matches, and flags go to human approvers only.
