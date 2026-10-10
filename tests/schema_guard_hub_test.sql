-- Guard tests for 004_approval_hub.sql: global kill switch, kill-switch auditing, manifest
-- versioning, edit/rework transitions, rejections, QA reports and hub identity tables.
-- Runs in one transaction, rolled back.
\set ON_ERROR_STOP 1
\set QUIET on
\pset tuples_only on
\pset format unaligned
BEGIN;

CREATE FUNCTION pg_temp.expect_error(stmt text, expected_state text, label text)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    BEGIN
        EXECUTE stmt;
    EXCEPTION WHEN OTHERS THEN
        IF SQLSTATE <> expected_state THEN
            RAISE EXCEPTION 'FAIL %: expected SQLSTATE %, got % (%)',
                label, expected_state, SQLSTATE, SQLERRM;
        END IF;
        RAISE NOTICE 'ok   %', label;
        RETURN;
    END;
    RAISE EXCEPTION 'FAIL %: statement succeeded but should have failed', label;
END;
$$;

CREATE FUNCTION pg_temp.expect_ok(stmt text, label text)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE stmt;
    RAISE NOTICE 'ok   %', label;
END;
$$;

CREATE FUNCTION pg_temp.check(cond boolean, label text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF NOT cond THEN
        RAISE EXCEPTION 'FAIL %', label;
    END IF;
    RAISE NOTICE 'ok   %', label;
END;
$$;

CREATE FUNCTION pg_temp.awaiting_job(p_id uuid, p_title text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO video_jobs (id, channel_id, origin) VALUES (p_id, 'hub-chan', 'owner');
    UPDATE video_jobs SET state = 'researching' WHERE id = p_id;
    UPDATE video_jobs SET state = 'scripting' WHERE id = p_id;
    UPDATE video_jobs SET state = 'rendering' WHERE id = p_id;
    UPDATE video_jobs SET state = 'qa' WHERE id = p_id;
    UPDATE video_jobs SET state = 'awaiting_approval', content_hash = 'sha256:' || repeat('a', 64),
        manifest = jsonb_build_object('metadata', jsonb_build_object('title', p_title))
    WHERE id = p_id;
END;
$$;

DO $$ BEGIN PERFORM set_config('app.actor', 'hub:guard', true); END $$;

INSERT INTO clusters (id, display_name) VALUES ('hub-clu', 'Hub cluster');
INSERT INTO google_projects (id, daily_quota_units, oauth_client_ref)
    VALUES ('proj-hub', 10000, 'vault://google/proj-hub#oauth_client');
INSERT INTO channels (id, display_name, storage_prefix, memory_namespace, cluster_id,
                      autonomy_tier, daily_budget_cents, google_project_id,
                      youtube_credentials_ref)
    VALUES ('hub-chan', 'Hub', 'channels/hub-chan/', 'mem:hub-chan', 'hub-clu', 'gated', 1000,
            'proj-hub', 'vault://channels/hub-chan/youtube#refresh_token');

-- ------------------------------------------------------------ manifest versioning
SELECT pg_temp.awaiting_job('00000000-0000-0000-0000-0000000000a1', 'v1');
SELECT pg_temp.check(
    (SELECT manifest_version = 1 FROM video_jobs WHERE id = '00000000-0000-0000-0000-0000000000a1')
    AND (SELECT count(*) = 1 FROM manifest_versions
         WHERE job_id = '00000000-0000-0000-0000-0000000000a1'),
    'first manifest is version 1 and recorded');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET manifest = '{"metadata": {"title": "sneaky"}}'
      WHERE id = '00000000-0000-0000-0000-0000000000a1'$$,
    '23514', 'manifest cannot change while awaiting approval');
SELECT pg_temp.expect_ok(
    $$UPDATE video_jobs SET state = 'qa' WHERE id = '00000000-0000-0000-0000-0000000000a1'$$,
    'edit: awaiting_approval -> qa');
SELECT pg_temp.expect_ok(
    $$UPDATE video_jobs SET manifest = '{"metadata": {"title": "v2"}}',
             content_hash = 'sha256:' || repeat('b', 64)
      WHERE id = '00000000-0000-0000-0000-0000000000a1'$$,
    'edited manifest stored during QA');
SELECT pg_temp.check(
    (SELECT manifest_version = 2 FROM video_jobs WHERE id = '00000000-0000-0000-0000-0000000000a1')
    AND (SELECT content_hash = 'sha256:' || repeat('b', 64) FROM manifest_versions
         WHERE job_id = '00000000-0000-0000-0000-0000000000a1' AND version = 2),
    'edit creates manifest version 2 with the new hash');
SELECT pg_temp.check(
    (SELECT manifest->'metadata'->>'title' = 'v1' FROM manifest_versions
     WHERE job_id = '00000000-0000-0000-0000-0000000000a1' AND version = 1),
    'previous version is kept for diffing');
SELECT pg_temp.check(
    NOT EXISTS (SELECT 1 FROM approvals WHERE job_id = '00000000-0000-0000-0000-0000000000a1'),
    'an edit never creates an approval');
SELECT pg_temp.expect_error(
    $$UPDATE manifest_versions SET manifest = '{}'$$, '42501', 'manifest versions are immutable');
SELECT pg_temp.expect_error(
    $$DELETE FROM manifest_versions$$, '42501', 'manifest versions cannot be deleted');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET manifest = '{"x": 1}', content_hash = NULL
      WHERE id = '00000000-0000-0000-0000-0000000000a1'$$,
    '23514', 'a manifest needs a content hash');

-- ------------------------------------------------------------ rework transitions
SELECT pg_temp.awaiting_job('00000000-0000-0000-0000-0000000000a2', 'rework');
SELECT pg_temp.expect_ok(
    $$UPDATE video_jobs SET state = 'scripting' WHERE id = '00000000-0000-0000-0000-0000000000a2'$$,
    'reject for rework: awaiting_approval -> scripting');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET state = 'scheduled' WHERE id = '00000000-0000-0000-0000-0000000000a2'$$,
    '23514', 'rework cannot jump to scheduled');

-- ------------------------------------------------------------ rejections
SELECT pg_temp.awaiting_job('00000000-0000-0000-0000-0000000000a3', 'reject');
SELECT pg_temp.expect_error(
    $$INSERT INTO job_rejections (job_id, channel_id, content_hash, reason, rework_stage, rejected_by)
      VALUES ('00000000-0000-0000-0000-0000000000a3', 'hub-chan', 'sha256:' || repeat('a', 64),
              'too short', 'script', 'approver1')$$,
    '23514', 'rejection needs a real reason');
SELECT pg_temp.expect_error(
    $$INSERT INTO job_rejections (job_id, channel_id, content_hash, reason, rework_stage, rejected_by)
      VALUES ('00000000-0000-0000-0000-0000000000a3', 'hub-chan', 'sha256:' || repeat('a', 64),
              'claims are not supported by sources', 'publish', 'approver1')$$,
    '23514', 'rejection needs a valid rework stage');
SELECT pg_temp.expect_error(
    $$INSERT INTO job_rejections (job_id, channel_id, content_hash, reason, rework_stage, rejected_by)
      VALUES ('00000000-0000-0000-0000-0000000000a3', 'hub-chan', 'sha256:' || repeat('c', 64),
              'claims are not supported by sources', 'script', 'approver1')$$,
    '23514', 'rejection of stale content is refused');
SELECT pg_temp.expect_error(
    $$INSERT INTO job_rejections (job_id, channel_id, content_hash, reason, rework_stage, rejected_by)
      VALUES ('00000000-0000-0000-0000-0000000000a2', 'hub-chan', 'sha256:' || repeat('a', 64),
              'claims are not supported by sources', 'script', 'approver1')$$,
    '23514', 'only jobs awaiting approval can be rejected');
SELECT pg_temp.expect_ok(
    $$INSERT INTO job_rejections (job_id, channel_id, content_hash, reason, rework_stage, rejected_by)
      VALUES ('00000000-0000-0000-0000-0000000000a3', 'hub-chan', 'sha256:' || repeat('a', 64),
              'claims are not supported by sources', 'script', 'approver1')$$,
    'valid rejection recorded');
SELECT pg_temp.check(
    EXISTS (SELECT 1 FROM audit_events WHERE action = 'approval.rejected'
            AND subject = 'job:00000000-0000-0000-0000-0000000000a3'),
    'rejection is audited');
SELECT pg_temp.expect_error($$DELETE FROM job_rejections$$, '42501', 'rejections are append-only');

-- ------------------------------------------------------------ QA reports
SELECT pg_temp.expect_error(
    $$INSERT INTO qa_reports (job_id, passed, risk_score)
      VALUES ('00000000-0000-0000-0000-0000000000a3', true, 101)$$,
    '23514', 'risk score is 0..100');
SELECT pg_temp.expect_error(
    $$INSERT INTO qa_reports (job_id, passed, risk_score, flags)
      VALUES ('00000000-0000-0000-0000-0000000000a3', true, 10, '{"a": 1}')$$,
    '23514', 'flags must be a JSON array');
SELECT pg_temp.expect_ok(
    $$INSERT INTO qa_reports (job_id, content_hash, passed, risk_score, flags, sources)
      VALUES ('00000000-0000-0000-0000-0000000000a3', 'sha256:' || repeat('a', 64), true, 10,
              '[{"claim": "x"}]', '[{"url": "https://example.test"}]')$$,
    'QA report recorded');
SELECT pg_temp.expect_error($$UPDATE qa_reports SET risk_score = 0$$, '42501',
    'QA reports are append-only');

-- ------------------------------------------------------------ global kill switch
SELECT pg_temp.awaiting_job('00000000-0000-0000-0000-0000000000a4', 'kill');
INSERT INTO approvals (job_id, channel_id, content_hash, token_jti, approver, expires_at)
    VALUES ('00000000-0000-0000-0000-0000000000a4', 'hub-chan', 'sha256:' || repeat('a', 64),
            'jti-hub-a4', 'approver1', now() + interval '1 hour');
UPDATE video_jobs SET state = 'approved' WHERE id = '00000000-0000-0000-0000-0000000000a4';
UPDATE global_controls SET publishing_paused = true;
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET state = 'scheduled' WHERE id = '00000000-0000-0000-0000-0000000000a4'$$,
    'YTC01', 'global kill switch blocks scheduling');
SELECT pg_temp.check(
    EXISTS (SELECT 1 FROM audit_events WHERE action = 'killswitch.engaged' AND subject = 'global'),
    'global kill switch is audited');
UPDATE global_controls SET publishing_paused = false;
SELECT pg_temp.expect_ok(
    $$UPDATE video_jobs SET state = 'scheduled' WHERE id = '00000000-0000-0000-0000-0000000000a4'$$,
    'released kill switch allows scheduling');
SELECT pg_temp.expect_error($$DELETE FROM global_controls$$, '42501',
    'global controls row cannot be deleted');
SELECT pg_temp.expect_error($$INSERT INTO global_controls (id) VALUES (false)$$, '23514',
    'there is exactly one global controls row');
UPDATE channels SET status = 'paused' WHERE id = 'hub-chan';
UPDATE clusters SET status = 'paused' WHERE id = 'hub-clu';
SELECT pg_temp.check(
    (SELECT count(*) = 2 FROM audit_events WHERE action = 'killswitch.engaged'
     AND subject IN ('channel:hub-chan', 'cluster:hub-clu')),
    'channel and cluster kill switches are audited');

-- ------------------------------------------------------------ hub identity
SELECT pg_temp.expect_error(
    $$INSERT INTO hub_users (username, display_name, role) VALUES ('mallory', 'M', 'superuser')$$,
    '23514', 'only approver, operator and admin roles exist');
SELECT pg_temp.expect_ok(
    $$INSERT INTO hub_users (username, display_name, role) VALUES ('approver1', 'A', 'approver')$$,
    'create approver');
SELECT pg_temp.check(
    EXISTS (SELECT 1 FROM audit_events WHERE action = 'hub.user.created'
            AND subject = 'user:approver1'),
    'user creation is audited');
SELECT pg_temp.expect_error($$DELETE FROM hub_users$$, '42501', 'hub users cannot be deleted');
SELECT pg_temp.expect_error(
    $$INSERT INTO hub_sessions (token_hash, user_id, csrf_hash, expires_at)
      SELECT '\x00'::bytea, id, sha256('c'), now() + interval '1 hour'
      FROM hub_users WHERE username = 'approver1'$$,
    '23514', 'sessions store a 32-byte token hash, never the token');
SELECT pg_temp.expect_ok(
    $$INSERT INTO hub_sessions (token_hash, user_id, csrf_hash, expires_at)
      SELECT sha256('t'), id, sha256('c'), now() + interval '1 hour'
      FROM hub_users WHERE username = 'approver1'$$,
    'session stored by hash');

SELECT pg_temp.check(audit_chain_first_break() IS NULL, 'audit chain still verifies');

\echo 'hub schema guard tests passed'
ROLLBACK;
