# Approval tiers: promoting and demoting a channel

Tier 1 = approve-all. Tier 2 = risk-based (fast lane for low-risk, deep review for the rest,
plus spot checks). No tier removes your approval or the gateway's token check.

## A promotion recommendation appears
The `tiers` job (every 15 minutes) or `ytctl tier evaluate` creates a recommendation when a
channel has met the rules in `config/platform.yaml`. Operators are alerted (`tier.recommended`).
1. Open the hub, **Tiers**. Read the evidence (days, decisions, rejection rate, risk, cost).
2. Look at the channel yourself: its last videos, scorecards, any warnings in YouTube Studio.
3. Confirm with a note (10+ characters) or dismiss with the reason. Only an admin can.
4. Watch the first week: the Tiers page shows spot-check disagreements.

## A channel was demoted
Cause is in the decision history (strike, rejection spike, policy flag). It is back on
approve-all, queued videos are no longer fast-laned. Fix the cause (see
[strike-response.md](strike-response.md)); a new recommendation comes only after the rules are
met again with a clean window.

## Record a strike, warning or takedown
`ytctl tier strike CHANNEL --kind strike --note "..."` or hub API `POST /api/channels/{id}/strikes`
(admin). It demotes at once and blocks promotion for `strike_free_days`.

## Spot-check disagreement is high
`ytctl tier report [--channel C]`: if humans overrule low-risk videos often, lower
`fast_lane_max_risk` (hub setting) or raise `spot_check_percent`, via a reviewed config change.
