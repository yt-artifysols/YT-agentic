# YouTube API quota

Quota is per **Google project** (default 10,000 units/day, reset at midnight Pacific). An upload
costs 1,600 units, so one project covers about six uploads a day. Each project serves a small
group of channels; the gateway charges the ledger before every call and refuses when exhausted.

**Detect**: `quota.high` alert at 80% (monitor), `ytcore_quota_used_ratio` in the dashboard,
`QuotaExhausted` failures in the gateway.

## Respond
1. Do **not** raise the ledger cap in the database to push uploads through: Google will reject
   the calls and the audit trail will lie.
2. Check what used it: `SELECT operation, sum(units) FROM quota_ledger WHERE google_project_id =
   '<p>' AND usage_date = youtube_quota_day() GROUP BY 1;` Unexpected `videos.list` volume means
   a polling bug; unexpected uploads mean a replan loop (look at `daily_plans`).
3. Affected jobs fail with `QuotaExhausted` and are re-planned; nothing is lost. They publish
   after the next quota day.
4. Spread load: move channels to another Google project (`channels.google_project_id`; one
   project per small channel group is the design) or shift slots (`channels.publish_hour_local`,
   `clusters.slot_spacing_minutes`).
5. Structural fix: request a quota extension from Google (audit form). That is a manual step
   for the account owner; track its status here.

## Verify
`ytctl ops stuck` shows no `quota.high`; the next scheduled upload succeeds.
