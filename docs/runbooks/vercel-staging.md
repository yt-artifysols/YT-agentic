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

## 1. Database
1. Vercel dashboard, Storage, add **Neon Postgres** (Marketplace). Use the **unpooled** URL for
   the owner steps below.
2. Migrate as the owner: `DATABASE_URL=<owner unpooled url> python -m ytcore.db.migrate`
3. Create one login per service (choose long random passwords; never reuse):
   ```sql
   CREATE ROLE hub_login  LOGIN PASSWORD '<random>'; GRANT yt_hub    TO hub_login;
   CREATE ROLE jobs_login LOGIN PASSWORD '<random>'; GRANT yt_worker TO jobs_login;
   CREATE ROLE ops_login  LOGIN PASSWORD '<random>'; GRANT yt_ops    TO ops_login;
   ```
   (or `ytctl ops grant-role --role yt_hub --login hub_login`). Build each service URL from the
   owner URL with that login, and use the **pooled** host for the services.

## 2. Approval key and cron secret (once)
`bash deploy/vercel/gen_secrets.sh` writes `.secrets-staging/` (key pair + `cron_secret`); paste into the
dashboards as Sensitive variables, then `shred -u` it. Or by hand:
```bash
openssl genpkey -algorithm ed25519 -out approval.pem
openssl pkey -in approval.pem -pubout -out approval.pub
```
Private key -> hub-api variable only. Public key -> jobs variable. Then `shred -u approval.pem`.

## 3. Vercel projects (Add New, Project, import `yt-artifysols/YT-agentic`)
Set the root directory as in the table. Python projects need no framework preset.

**yt-agentic-hub-api** (Production + Preview, mark secrets Sensitive):
`ENV=staging`, `DATABASE_URL` (hub_login, pooled), `YT_HUB_SIGNING_KEY_PEM` (private key),
`YT_HUB_ORIGIN=https://<ui domain>`, `YT_HUB_RP_ID=<ui host>`, `YT_HUB_COOKIE_SECURE=true`,
`YT_HUB_TRUST_FORWARDED_FOR=true`, `YT_HUB_REQUIRE_STEPUP=true`,
`JOBS_WAKE_URL=https://<jobs domain>/api/cron/tick`, `CRON_SECRET=<same value as jobs>`.

**yt-agentic-jobs**:
`ENV=staging`, `YT_SECRETS_BACKEND=platform_env`, `DATABASE_URL` (jobs_login, pooled),
`OPS_DATABASE_URL` (ops_login), `APPROVAL_PUBLIC_KEY` (public key PEM),
`CRON_SECRET=<random 32+ chars, same as hub-api>`.

**yt-agentic-hub-ui**: `HUB_API_URL=https://<hub-api domain>`.

Passkeys are bound to the UI host (`YT_HUB_RP_ID`): attach the final domain before registering.
Vercel Cron on the Hobby plan is limited to daily runs, so jobs advance on the daily run, when
a decision is made (the hub pokes the driver), and when the planner runs; Pro allows
`* * * * *` in `deploy/vercel/jobs/vercel.json`.

## 4. Bootstrap (from your machine, owner DSN, `ENV=staging YT_SECRETS_BACKEND=platform_env`)
```bash
export ENV=staging YT_SECRETS_BACKEND=platform_env DATABASE_URL=<owner unpooled url>
ytctl channel apply --config-dir config/examples    # channels, clusters, draft brand/policy
ytctl ops seed-staging                              # licences for the fake media providers
python -m ytcore.approvals.service.cli invite --username owner --name "Owner" --role admin
```
Open the UI, `/login?invite=<code>`, register your passkey, then approve the proposed brand and
policy documents under **Brand & policy**.

## 5. Verify
- UI loads, sign-in with the passkey works, Today shows the fleet.
- `curl -H "Authorization: Bearer $CRON_SECRET" https://<jobs domain>/api/cron/plan` plans and
  produces jobs; they appear in **Review** as "Needs your approval".
- Approve one: it goes live in the fake YouTube client; the audit trail shows hub.approve,
  approval.consumed, upload.live. Nothing is ever published without that approval.

## Limits (staging only)
Assets are limited to 5 MiB each in Postgres; there is no render sandbox; real YouTube upload
needs the separate gateway (not on Vercel); in-process rate limiting is per function instance
(add Vercel Firewall rate limits on `/api/auth/*`).
