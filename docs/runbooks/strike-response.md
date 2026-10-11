# Strike or policy action on a channel

Treat any strike, warning, demonetisation notice or takedown as a **channel incident**.

## Contain (within the hour)
0. Record it: `ytctl tier strike CHANNEL --note "..."`. The channel returns to approve-all at once
   and cannot be promoted again for 90 days ([approval-tiers.md](approval-tiers.md)).
1. Pause the **channel** ([kill-switch.md](kill-switch.md)); pause the **cluster** if the notice
   names a pattern that other channels in it share (reused media, a template, a topic).
2. Cancel its scheduled/awaiting jobs by letting deadlines pass or signalling `kill`; do not
   approve anything for it meanwhile.

## Understand
3. Read the notice in YouTube Studio: which video, which policy, which timestamp.
4. Find the job: `SELECT job_id FROM uploads WHERE youtube_video_id = '<id>'` then the QA
   report (`qa_reports`, `job_risk`), the approval (`approvals`: who approved, when), the
   manifest and the sources. Decide: policy gap in our rules, a human approval miss, or a
   platform judgement call.
5. Check siblings: did other channels publish similar content (`content_fingerprints` shows
   cross-channel duplicates; scorecards show policy flags)?

## Fix
6. Rule gap -> propose a policy / banned-claims knowledge draft for the channel (a human
   approves it in the hub) and add a deterministic rule in `ytcore/safety/checks.py` with a
   test; update `docs/policy-sources.md` with the policy text and version you relied on.
7. Appeal in Studio if the decision is wrong (account owner only).
8. Remove or unlist the video if the policy requires it (Studio).

## Resume
9. Resume only when the owner agrees. Run the first videos with **approve-all** scrutiny
   (tier 1: nothing fast-laned) for a week. Record the incident: date, video, policy, root cause,
   changes made.

Three strikes lead to termination: after the second, stop publishing on the channel until the
owner and a second reviewer have audited its last 20 videos.
