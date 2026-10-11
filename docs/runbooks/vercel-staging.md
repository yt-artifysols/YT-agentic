# Staging on Vercel (hub UI + hub API + job driver + Postgres)

Everything runs on Vercel and a Vercel-Marketplace Postgres (Neon). This is **staging**: fake
media providers, the fake YouTube client (nothing is uploaded), assets in Postgres, the approval
key in an encrypted variable. Production keeps Temporal, KMS and the separate gateway
(ADR 0013); the serverless runtime refuses `ENV=production`. Design: ADR 0014.

| Vercel project | Root directory | Holds | Database login |
|---|---|---|---|
| `yt-agentic-hub-ui` | `ui/hub` (Next.js) | nothing secret; proxies `/api` to the hub API | none |
| `yt-agentic-hub-api` | `deploy/vercel/hub-api` | approval PRIVATE key | `yt_hub` |
| `yt-agentic-jobs` | `deploy/vercel/jobs` | approval PUBLIC key, cron secret | `yt_worker`, `yt_ops` |
| `yt-agentic` (exists) | repo root | static site + `/hub-demo` | none |

Only the hub project can sign or record an approval; the jobs project cannot (database roles).

## 1. Setup order
1. Create the three projects (roots in the table above) and add **Neon Postgres** from the Vercel
   Marketplace (Storage). The integration injects the OWNER connection into the project it is
   linked to: unlink the Neon resource from `yt-agentic-hub-api` and `yt-agentic-jobs` (the
   bootstrap also deletes those variables).
2. Secrets: `bash deploy/vercel/gen_secrets.sh`, paste into the dashboards as Sensitive
   variables, then `shred -u .secrets-staging/*`:
   `YT_HUB_SIGNING_KEY_PEM` (private key, hub-api only), `APPROVAL_PUBLIC_KEY` (jobs only),
   `CRON_SECRET` (both).
3. Plain variables (already set by the setup): hub-api `ENV=staging`, `YT_HUB_ORIGIN`,
   `YT_HUB_RP_ID`, `YT_HUB_COOKIE_SECURE=true`, `YT_HUB_TRUST_FORWARDED_FOR=true`,
   `YT_HUB_REQUIRE_STEPUP=true`, `JOBS_WAKE_URL`; jobs `ENV=staging`,
   `YT_SECRETS_BACKEND=platform_env`; ui `HUB_API_URL`.
4. Bootstrap (one command; needs `OWNER_DATABASE_URL_UNPOOLED` = Neon's `DATABASE_URL_UNPOOLED` and
   `VERCEL_API_TOKEN`, both as environment secrets, never in chat):
   `python deploy/vercel/bootstrap.py`. It migrates, creates the three limited logins, sets
   `YT_HUB_DATABASE_URL` / `YT_JOBS_DATABASE_URL` / `YT_OPS_DATABASE_URL` / `YT_DATABASE_URL_REF`,
   applies `config/examples`, registers the fake providers' licences and prints one admin invite.
   Re-running rotates the three passwords. The services refuse to start on the owner login.
5. Redeploy hub-api and jobs. Open the UI, `/login?invite=<code>`, register your passkey, then
   approve the proposed brand and policy documents under **Brand & policy**.

Passkeys are bound to the UI host (`YT_HUB_RP_ID`): attach the final domain before registering.
Vercel Cron on the Hobby plan is limited to daily runs, so jobs advance on the daily run, when
a decision is made (the hub pokes the driver), and when the planner runs; Pro allows
`* * * * *` in `deploy/vercel/jobs/vercel.json`.

## 2. Verify
- UI loads, sign-in with the passkey works, Today shows the fleet.
- `curl -H "Authorization: Bearer $CRON_SECRET" https://yt-agentic-jobs.vercel.app/api/cron/plan`
  plans and produces jobs; they appear in **Review** as "Needs your approval".
- Approve one: it goes live in the fake YouTube client; the audit trail shows hub.approve,
  approval.consumed, upload.live. Nothing is ever published without that approval.

## Limits (staging only)
Assets are limited to 5 MiB each in Postgres; there is no render sandbox; real YouTube upload
needs the separate gateway (not on Vercel); in-process rate limiting is per function instance
(add Vercel Firewall rate limits on `/api/auth/*`).
