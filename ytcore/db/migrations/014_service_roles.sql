-- Phase 8: one database role per service, so a compromised worker (the process that handles
-- untrusted content) cannot forge human-only actions. Until now every service shared one
-- credential and the human-only guards trusted a self-asserted `app.actor`; now the worker and
-- gateway roles simply have no privilege to approve, sign off, release a kill switch or change a
-- tier. Services connect with a login user that is a member of exactly one of these roles
-- (`ytctl ops grant-role`). Migrations still run as the owner.
--
--   yt_hub     the human control plane: approvals, knowledge approval, kill switches, tiers,
--              rollout, sessions. Cannot write uploads or the quota ledger.
--   yt_worker  production: jobs, assets, QA, costs, memory, drafts. No approvals, no controls.
--   yt_gateway the only writer of uploads and the quota ledger; moves jobs through publishing.
--   yt_ops     scheduled jobs: planner, monitor, breaker (pauses only), analytics, tiers,
--              reconciliation.
DO $$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['yt_hub', 'yt_worker', 'yt_gateway', 'yt_ops'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
    END LOOP;
END $$;

-- Trigger helpers that write on behalf of every caller run with the owner's rights, so the
-- caller needs no direct privilege on the event, audit and cost tables.
ALTER FUNCTION audit(text, text, jsonb) SECURITY DEFINER SET search_path = public, pg_temp;
ALTER FUNCTION video_jobs_record_event() SECURITY DEFINER SET search_path = public, pg_temp;
ALTER FUNCTION video_jobs_record_manifest() SECURITY DEFINER SET search_path = public, pg_temp;
ALTER FUNCTION job_costs_budget() SECURITY DEFINER SET search_path = public, pg_temp;
ALTER FUNCTION job_costs_job_budget() SECURITY DEFINER SET search_path = public, pg_temp;
ALTER FUNCTION job_costs_apply() SECURITY DEFINER SET search_path = public, pg_temp;
ALTER FUNCTION quota_ledger_check() SECURITY DEFINER SET search_path = public, pg_temp;
-- The tier guards write history, alerts and supersede recommendations as the owner, so the
-- ops role needs no privilege on those tables (and cannot confirm a recommendation).
ALTER FUNCTION channels_tier_guard() SECURITY DEFINER SET search_path = public, pg_temp;
ALTER FUNCTION channel_strikes_demote() SECURITY DEFINER SET search_path = public, pg_temp;
ALTER FUNCTION job_owner_inputs_link() SECURITY DEFINER SET search_path = public, pg_temp;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO yt_hub, yt_worker, yt_gateway, yt_ops;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO yt_hub, yt_worker, yt_gateway, yt_ops;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO yt_hub, yt_worker, yt_gateway, yt_ops;

-- Session and passkey material is the hub's alone.
REVOKE SELECT ON hub_sessions, hub_invites, webauthn_credentials
    FROM yt_worker, yt_gateway, yt_ops;

-- ------------------------------------------------------------------ hub
GRANT INSERT, UPDATE, DELETE ON
    approvals, channels, clusters, google_projects, global_controls, manifest_versions,
    qa_reports, job_rejections, hub_users, webauthn_credentials, hub_sessions, hub_invites,
    hub_notifications, knowledge_docs, job_knowledge, owner_inputs, job_owner_inputs,
    rights_register, alerts, cluster_breaker_config, rollout_signoffs, pool_limits,
    tier_rules, channel_strikes, tier_recommendations, tier_changes, job_review_routing,
    review_outcomes, video_jobs, job_events, job_costs, job_assets, daily_plans
    TO yt_hub;

-- ------------------------------------------------------------------ worker
GRANT INSERT, UPDATE ON
    video_jobs, manifest_versions, qa_reports, job_assets, job_risk, content_fingerprints,
    channel_memory, alerts, job_costs, cluster_health, job_knowledge, job_owner_inputs,
    pool_waiters, pool_leases
    TO yt_worker;
GRANT UPDATE ON owner_inputs TO yt_worker;
-- a cancelled job asks for its own re-plan (the planner, an ops job, does the re-planning)
GRANT UPDATE (status, replan_reason, updated_at) ON daily_plans TO yt_worker;
GRANT INSERT ON knowledge_docs TO yt_worker;  -- agents propose DRAFTS; approving is the hub's
GRANT DELETE ON pool_waiters, pool_leases, channel_memory TO yt_worker;

-- ------------------------------------------------------------------ gateway
GRANT INSERT, UPDATE ON uploads, quota_ledger, video_jobs, alerts, job_costs TO yt_gateway;

-- ------------------------------------------------------------------ ops
GRANT INSERT, UPDATE ON
    daily_plans, video_jobs, alerts, breaker_trips, video_metrics, performance_notes,
    daily_scorecards, uploads, channel_strikes, review_outcomes, job_risk
    TO yt_ops;
GRANT INSERT ON tier_recommendations TO yt_ops;  -- the evaluator recommends; only the hub decides
-- The circuit breaker may only ever pause; tiers only ever change through the guarded trigger.
GRANT UPDATE (status) ON clusters, channels TO yt_ops;
GRANT UPDATE (approval_tier, approval_tier_since) ON channels TO yt_ops;
