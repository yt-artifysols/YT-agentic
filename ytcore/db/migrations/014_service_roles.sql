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
--   yt_backup  read-only, for the nightly dump (the only non-hub role that reads passkeys)
--   yt_ops     scheduled jobs: planner, monitor, breaker (pauses only), analytics, tiers,
--              reconciliation.
DO $$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['yt_hub', 'yt_worker', 'yt_gateway', 'yt_ops', 'yt_backup'] LOOP
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

-- Every trigger that writes an audit record does so as the owner, and nobody else can call
-- audit() directly: a service cannot add (or forge) audit records by hand.
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT p.oid::regprocedure AS sig FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.prorettype = 'trigger'::regtype
          AND p.prosrc ~ 'audit\(' AND NOT p.prosecdef
    LOOP
        EXECUTE format('ALTER FUNCTION %s SECURITY DEFINER SET search_path = public, pg_temp',
                       r.sig);
    END LOOP;
END $$;
REVOKE EXECUTE ON FUNCTION audit(text, text, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION audit(text, text, jsonb) TO yt_hub;  -- the hub records its own decisions
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- A claimed `hub:<name>` actor only counts when the connection really is the hub's login;
-- from any other login it is recorded as untrusted, so a compromised worker cannot attribute
-- its rows to a human.
CREATE OR REPLACE FUNCTION app_actor() RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE
    claimed text := nullif(current_setting('app.actor', true), '');
BEGIN
    IF claimed IS NULL THEN
        RETURN current_user::text;
    END IF;
    IF claimed LIKE 'hub:%' AND NOT pg_has_role(session_user, 'yt_hub', 'USAGE') THEN
        RETURN 'untrusted:' || claimed;
    END IF;
    RETURN claimed;
END;
$$;

-- A pause can be lifted only by the hub: the ops role may set `paused`, never resume.
CREATE FUNCTION status_resume_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'active' AND NEW.status = 'active'
       AND NOT pg_has_role(current_user, 'yt_hub', 'USAGE') THEN
        RAISE EXCEPTION 'only the hub can resume a paused %', TG_TABLE_NAME
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER clusters_resume_guard BEFORE UPDATE OF status ON clusters
    FOR EACH ROW EXECUTE FUNCTION status_resume_guard();
CREATE TRIGGER channels_resume_guard BEFORE UPDATE OF status ON channels
    FOR EACH ROW EXECUTE FUNCTION status_resume_guard();

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
-- Column-level on video_jobs: production moves a job forward; it cannot touch anything else.
GRANT INSERT ON video_jobs TO yt_worker;
GRANT UPDATE (state, manifest, manifest_version, content_hash, approval_deadline, updated_at)
    ON video_jobs TO yt_worker;
GRANT INSERT, UPDATE ON qa_reports, job_assets, job_risk, content_fingerprints, channel_memory,
    alerts, cluster_health, job_knowledge, pool_waiters, pool_leases TO yt_worker;
GRANT INSERT ON job_costs, job_owner_inputs, manifest_versions TO yt_worker;
-- Claiming inputs uses SELECT ... FOR UPDATE, which needs UPDATE on one column; nothing else.
GRANT UPDATE (updated_at) ON owner_inputs TO yt_worker;
GRANT INSERT ON knowledge_docs TO yt_worker;  -- agents propose DRAFTS; approving is the hub's
GRANT DELETE ON pool_waiters, pool_leases, channel_memory TO yt_worker;
-- a cancelled job asks for its own re-plan (the planner, an ops job, does the re-planning)
GRANT UPDATE (status, replan_reason, updated_at) ON daily_plans TO yt_worker;

-- ------------------------------------------------------------------ gateway
GRANT INSERT, UPDATE ON uploads, quota_ledger, alerts TO yt_gateway;
GRANT INSERT ON job_costs TO yt_gateway;
GRANT UPDATE (state, updated_at) ON video_jobs TO yt_gateway;

-- ------------------------------------------------------------------ ops
-- The planner creates jobs; the monitor, breaker, analytics and tier jobs record their results.
-- No uploads and no review outcomes (the evidence tiers are judged on): those are the gateway's
-- and the hub's. Reconciling an upload by hand needs an admin login, not a scheduled job.
GRANT INSERT ON video_jobs TO yt_ops;
GRANT UPDATE (state, updated_at) ON video_jobs TO yt_ops;
GRANT INSERT, UPDATE ON daily_plans, alerts, breaker_trips, video_metrics, performance_notes,
    daily_scorecards TO yt_ops;
GRANT INSERT ON channel_strikes, tier_recommendations TO yt_ops;
-- The circuit breaker may only ever pause (the resume guard above enforces it).
GRANT UPDATE (status) ON clusters, channels TO yt_ops;
GRANT UPDATE (approval_tier, approval_tier_since) ON channels TO yt_ops;

-- ------------------------------------------------------------------ backup
-- Read-only, for the nightly dump (sessions hold only token digests, never usable tokens).
GRANT SELECT ON ALL TABLES IN SCHEMA public TO yt_backup;
GRANT USAGE ON SCHEMA public TO yt_backup;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO yt_backup;
