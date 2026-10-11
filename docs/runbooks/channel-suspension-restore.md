# Channel suspended or terminated

## Suspended / limited
1. Pause the channel and its cluster ([kill-switch.md](kill-switch.md)). Stop planning.
2. The owner follows YouTube's appeal flow with evidence from our records: the approvals
   (named human approver per video), sources, QA reports, synthetic-media disclosures
   (`uploads.synthetic_disclosure`), license references. Export with
   `SELECT * FROM audit_events WHERE subject LIKE 'job:%'` for the relevant jobs.
3. Do not create replacement channels to evade a suspension: that violates platform policy
   and puts the whole account at risk.

## Reinstated
4. Re-link OAuth if tokens were revoked ([key-rotation.md](key-rotation.md)).
5. Review and re-approve knowledge (brand voice, banned claims, policy) with the owner.
6. Resume the channel with the **channel in `shadow`** first (`autonomy_tier = 'shadow'`: the
   whole pipeline runs and nothing can publish), run several shadow jobs, review them, then
   switch to `gated` and publish one video at a time for a week.

## Terminated
7. Keep the data (audit trail, assets, analytics are append-only and retained). Archive the
   channel: pause it permanently; do not delete rows (history and audit chain depend on them).
8. Move its Google project's quota to other channels if useful. Revoke its OAuth grants and
   disable its secrets after the retention period.
