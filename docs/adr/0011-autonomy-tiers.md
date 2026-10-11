# ADR 0011: Risk-based approval tiers

- Status: Accepted
- Date: 2026-10-11

## Context
Reviewing every video of up to 20 channels one by one does not scale, but the human approval
step may never go away. Review effort should follow risk and track record.

## Decision
- **Two tiers per channel** (`channels.approval_tier`, migration 013). Tier 1 = approve-all (the
  default for every channel). Tier 2 = risk-based: low-risk, unflagged, QA-passed videos go to a
  batch fast lane (one tap, still one Ed25519 token per video, each bound to job, channel and
  content hash); everything else, and a configurable share of low-risk videos (spot checks), gets
  deep review. The tier is separate from `autonomy_tier` (shadow/gated); there is still **no
  auto-publish tier**.
- **A tier only decides how a human reviews.** The tier is read by the hub to pick the review
  lane and by nothing else. The gateway, the token code, the approval gate triggers and the
  workflow never read it (a structural test enforces this). Batch approval re-checks the lane at
  decision time, so a demotion applies to videos already in the queue.
- **Promotion is a recommendation** (`tier_recommendations`) created by the evaluator when the
  rules in `config/platform.yaml` hold (days observed, decisions, rejection and edit rate,
  average risk, cost per video, no strike or policy flag). Only an active hub admin can confirm
  it, with a note; the database refuses the tier change without a recommendation confirmed in
  the same transaction, and refuses shadow channels.
- **Demotion is automatic and immediate**: a recorded strike/warning/takedown (DB trigger), a
  rejection spike or a blocking policy flag (evaluated after every human decision and by the
  `tiers` scheduled job). Every change needs a recorded reason, is written to the append-only
  `tier_changes`, the audit trail and an operator alert. `tier_changes` can only be written by
  the channels trigger.
- **Spot checks** are drawn once per content version and stored (`job_review_routing`).
  `review_outcomes` records each human decision next to the predicted lane and risk; the
  disagreement report (spot checks, all low-risk videos, per risk bucket) feeds recalibration of
  the fast-lane threshold. It is reported, never auto-applied.
- **Dashboard**: the hub's Tiers page shows every channel's tier, recommendations (admin
  actions), the risk-vs-human report and the decision history with reasons.

## Consequences
- Existing fast-lane behaviour is now opt-in per channel; nothing is fast-laned until an admin
  promotes a channel.
- Strike data is entered by an admin/operator (hub or `ytctl tier strike`); there is no YouTube
  API source for it.
- Rules live in platform config; changing them needs a reviewed PR and `ytctl channel apply`.
