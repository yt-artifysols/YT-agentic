-- Guard tests for 003_publish_platform.sql: channel/cluster/tier gating, uploads, quota ledger,
-- cost budget, actor attribution and the audit trail. Runs in one transaction, rolled back.
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

-- Walk a job from planned to approved with a valid approval (jti = 'jti-' || label).
CREATE FUNCTION pg_temp.approved_job(p_id uuid, p_channel text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO video_jobs (id, channel_id, origin) VALUES (p_id, p_channel, 'owner');
    UPDATE video_jobs SET state = 'researching' WHERE id = p_id;
    UPDATE video_jobs SET state = 'scripting' WHERE id = p_id;
    UPDATE video_jobs SET state = 'rendering' WHERE id = p_id;
    UPDATE video_jobs SET state = 'qa' WHERE id = p_id;
    UPDATE video_jobs SET state = 'awaiting_approval', content_hash = 'sha256:' || repeat('a', 64),
        manifest = '{"metadata": {"title": "t"}, "assets": {}}' WHERE id = p_id;
    INSERT INTO approvals (job_id, channel_id, content_hash, token_jti, approver, expires_at)
        VALUES (p_id, p_channel, 'sha256:' || repeat('a', 64), 'jti-' || p_id,
                'owner@example.test', now() + interval '1 hour');
    UPDATE video_jobs SET state = 'approved' WHERE id = p_id;
END;
$$;

DO $$ BEGIN PERFORM set_config('app.actor', 'guard-test', true); END $$;

INSERT INTO clusters (id, display_name) VALUES ('clu-1', 'Cluster 1'), ('clu-2', 'Cluster 2');
INSERT INTO google_projects (id, daily_quota_units, oauth_client_ref) VALUES
    ('proj-alpha', 2000, 'vault://google/proj-alpha#oauth_client'),
    ('proj-beta', 2000, 'vault://google/proj-beta#oauth_client');
INSERT INTO channels (id, display_name, storage_prefix, memory_namespace, cluster_id,
                      autonomy_tier, daily_budget_cents, google_project_id,
                      youtube_credentials_ref) VALUES
    ('gated', 'Gated', 'channels/gated/', 'mem:gated', 'clu-1', 'gated', 1000, 'proj-alpha',
     'vault://channels/gated/youtube#refresh_token'),
    ('shadow', 'Shadow', 'channels/shadow/', 'mem:shadow', 'clu-1', 'shadow', 1000, 'proj-alpha',
     'vault://channels/shadow/youtube#refresh_token'),
    ('paused', 'Paused', 'channels/paused/', 'mem:paused', 'clu-1', 'gated', 1000, 'proj-alpha',
     'vault://channels/paused/youtube#refresh_token'),
    ('in-paused-cluster', 'C2', 'channels/c2/', 'mem:c2', 'clu-2', 'gated', 1000, 'proj-alpha',
     'vault://channels/c2/youtube#refresh_token');

-- ------------------------------------------------------------ defaults and references
INSERT INTO channels (id, display_name, storage_prefix, memory_namespace)
    VALUES ('fresh', 'Fresh', 'channels/fresh/', 'mem:fresh');
SELECT pg_temp.check((SELECT autonomy_tier = 'shadow' AND daily_budget_cents = 0
                      FROM channels WHERE id = 'fresh'),
    'new channels default to shadow tier with zero budget');
SELECT pg_temp.expect_error(
    $$UPDATE channels SET youtube_credentials_ref = 'ya29.literal-token' WHERE id = 'fresh'$$,
    '23514', 'credentials must be a vault/env reference');
SELECT pg_temp.expect_error(
    $$UPDATE channels SET autonomy_tier = 'auto' WHERE id = 'fresh'$$,
    '23514', 'there is no auto-publish tier');

-- ------------------------------------------------------------ publish gating
SELECT pg_temp.approved_job('00000000-0000-0000-0000-000000000001', 'gated');
SELECT pg_temp.approved_job('00000000-0000-0000-0000-000000000002', 'shadow');
SELECT pg_temp.approved_job('00000000-0000-0000-0000-000000000003', 'paused');
SELECT pg_temp.approved_job('00000000-0000-0000-0000-000000000004', 'in-paused-cluster');
UPDATE channels SET status = 'paused' WHERE id = 'paused';
UPDATE clusters SET status = 'paused' WHERE id = 'clu-2';

SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET state = 'scheduled' WHERE id = '00000000-0000-0000-0000-000000000002'$$,
    'YTC01', 'shadow-tier channel cannot schedule');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET state = 'scheduled' WHERE id = '00000000-0000-0000-0000-000000000003'$$,
    'YTC01', 'paused channel cannot schedule');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET state = 'scheduled' WHERE id = '00000000-0000-0000-0000-000000000004'$$,
    'YTC01', 'channel in paused cluster cannot schedule');
SELECT pg_temp.expect_ok(
    $$UPDATE video_jobs SET state = 'scheduled' WHERE id = '00000000-0000-0000-0000-000000000001'$$,
    'gated active channel schedules');

-- Pausing after scheduling still blocks publishing.
UPDATE channels SET status = 'paused' WHERE id = 'gated';
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET state = 'publishing' WHERE id = '00000000-0000-0000-0000-000000000001'$$,
    'YTC01', 'channel paused after scheduling cannot publish');
UPDATE channels SET status = 'active' WHERE id = 'gated';

-- ------------------------------------------------------------ uploads
SELECT pg_temp.expect_error(
    $$INSERT INTO uploads (job_id, channel_id, idempotency_key, content_hash, approval_jti,
                           google_project_id, synthetic_disclosure)
      VALUES ('00000000-0000-0000-0000-000000000001', 'gated', 'upl_' || repeat('1', 64),
              'sha256:' || repeat('a', 64), 'jti-00000000-0000-0000-0000-000000000001',
              'proj-alpha', true)$$,
    'YTU01', 'uploads require state publishing');
SELECT pg_temp.expect_ok(
    $$UPDATE video_jobs SET state = 'publishing' WHERE id = '00000000-0000-0000-0000-000000000001'$$,
    'scheduled -> publishing');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET state = 'cancelled' WHERE id = '00000000-0000-0000-0000-000000000001'$$,
    '23514', 'publishing cannot be cancelled');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET state = 'published' WHERE id = '00000000-0000-0000-0000-000000000001'$$,
    'YTU01', 'published requires a completed upload');
SELECT pg_temp.expect_error(
    $$INSERT INTO uploads (job_id, channel_id, idempotency_key, content_hash, approval_jti,
                           google_project_id, synthetic_disclosure)
      VALUES ('00000000-0000-0000-0000-000000000001', 'gated', 'upl_' || repeat('1', 64),
              'sha256:' || repeat('a', 64), 'jti-00000000-0000-0000-0000-000000000002',
              'proj-alpha', true)$$,
    '42501', 'upload needs an approval for this job');
SELECT pg_temp.expect_error(
    $$INSERT INTO uploads (job_id, channel_id, idempotency_key, content_hash, approval_jti,
                           google_project_id, synthetic_disclosure)
      VALUES ('00000000-0000-0000-0000-000000000001', 'gated', 'upl_' || repeat('1', 64),
              'sha256:' || repeat('b', 64), 'jti-00000000-0000-0000-0000-000000000001',
              'proj-alpha', true)$$,
    'YTU01', 'upload content hash must match the job');
SELECT pg_temp.expect_error(
    $$INSERT INTO uploads (job_id, channel_id, idempotency_key, content_hash, approval_jti,
                           google_project_id, synthetic_disclosure)
      VALUES ('00000000-0000-0000-0000-000000000001', 'gated', 'upl_' || repeat('1', 64),
              'sha256:' || repeat('a', 64), 'jti-00000000-0000-0000-0000-000000000001',
              'proj-beta', true)$$,
    'YTU01', 'upload must use the channel Google project');
SELECT pg_temp.expect_error(
    $$INSERT INTO uploads (job_id, channel_id, idempotency_key, content_hash, approval_jti,
                           google_project_id, synthetic_disclosure, status, youtube_video_id)
      VALUES ('00000000-0000-0000-0000-000000000001', 'gated', 'upl_' || repeat('1', 64),
              'sha256:' || repeat('a', 64), 'jti-00000000-0000-0000-0000-000000000001',
              'proj-alpha', true, 'uploaded', 'vid')$$,
    'YTU01', 'uploads start pending');
SELECT pg_temp.expect_ok(
    $$INSERT INTO uploads (job_id, channel_id, idempotency_key, content_hash, approval_jti,
                           google_project_id, synthetic_disclosure)
      VALUES ('00000000-0000-0000-0000-000000000001', 'gated', 'upl_' || repeat('1', 64),
              'sha256:' || repeat('a', 64), 'jti-00000000-0000-0000-0000-000000000001',
              'proj-alpha', true)$$,
    'valid pending upload');
SELECT pg_temp.expect_error(
    $$INSERT INTO uploads (job_id, channel_id, idempotency_key, content_hash, approval_jti,
                           google_project_id, synthetic_disclosure)
      VALUES ('00000000-0000-0000-0000-000000000001', 'gated', 'upl_' || repeat('2', 64),
              'sha256:' || repeat('a', 64), 'jti-00000000-0000-0000-0000-000000000001',
              'proj-alpha', true)$$,
    '23505', 'only one active upload per job');
SELECT pg_temp.expect_error(
    $$UPDATE uploads SET status = 'live' WHERE idempotency_key = 'upl_' || repeat('1', 64)$$,
    'YTU01', 'pending cannot jump to live');
SELECT pg_temp.expect_error(
    $$UPDATE uploads SET status = 'uploaded' WHERE idempotency_key = 'upl_' || repeat('1', 64)$$,
    '23514', 'uploaded requires a video id');
SELECT pg_temp.expect_error(
    $$UPDATE uploads SET synthetic_disclosure = false
      WHERE idempotency_key = 'upl_' || repeat('1', 64)$$,
    'YTU01', 'upload identity (incl. disclosure flag) is immutable');
SELECT pg_temp.expect_ok(
    $$UPDATE uploads SET status = 'uploaded', youtube_video_id = 'yt-vid-1'
      WHERE idempotency_key = 'upl_' || repeat('1', 64)$$, 'pending -> uploaded');
SELECT pg_temp.expect_error(
    $$UPDATE uploads SET youtube_video_id = 'yt-vid-2'
      WHERE idempotency_key = 'upl_' || repeat('1', 64)$$,
    'YTU01', 'video id is write-once');
SELECT pg_temp.expect_error(
    $$DELETE FROM uploads$$, '42501', 'uploads cannot be deleted');
SELECT pg_temp.expect_ok(
    $$UPDATE video_jobs SET state = 'published' WHERE id = '00000000-0000-0000-0000-000000000001'$$,
    'publishing -> published with a completed upload');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET state = 'live' WHERE id = '00000000-0000-0000-0000-000000000001'$$,
    'YTU01', 'live requires a verified live upload');
SELECT pg_temp.expect_ok(
    $$UPDATE uploads SET status = 'live' WHERE idempotency_key = 'upl_' || repeat('1', 64)$$,
    'uploaded -> live');
SELECT pg_temp.expect_ok(
    $$UPDATE video_jobs SET state = 'live' WHERE id = '00000000-0000-0000-0000-000000000001'$$,
    'published -> live');

-- ------------------------------------------------------------ quota ledger
SELECT pg_temp.expect_ok(
    $$INSERT INTO quota_ledger (google_project_id, units, operation)
      VALUES ('proj-alpha', 1600, 'videos.insert')$$, 'charge within quota');
SELECT pg_temp.expect_error(
    $$INSERT INTO quota_ledger (google_project_id, units, operation)
      VALUES ('proj-alpha', 1600, 'videos.insert')$$,
    'YTQ01', 'quota exhausted blocks the charge');
SELECT pg_temp.expect_ok(
    $$INSERT INTO quota_ledger (google_project_id, units, operation)
      VALUES ('proj-beta', 1600, 'videos.insert')$$, 'quota is per Google project');
SELECT pg_temp.expect_error($$UPDATE quota_ledger SET units = 1$$, '42501',
    'quota_ledger rows cannot be updated');
SELECT pg_temp.expect_error($$DELETE FROM quota_ledger$$, '42501',
    'quota_ledger rows cannot be deleted');

-- ------------------------------------------------------------ cost ledger and budget
SELECT pg_temp.expect_ok(
    $$INSERT INTO job_costs (job_id, channel_id, kind, cost_cents)
      VALUES ('00000000-0000-0000-0000-000000000001', 'gated', 'render', 600)$$,
    'cost within budget');
SELECT pg_temp.check((SELECT render_cost_cents = 600 FROM video_jobs
                      WHERE id = '00000000-0000-0000-0000-000000000001'),
    'render cost accumulates on the job');
SELECT pg_temp.expect_error(
    $$INSERT INTO job_costs (job_id, channel_id, kind, cost_cents)
      VALUES ('00000000-0000-0000-0000-000000000001', 'gated', 'render', 500)$$,
    'YTB01', 'daily channel budget blocks overspend');
SELECT pg_temp.expect_error(
    $$INSERT INTO job_costs (job_id, channel_id, kind, cost_cents)
      VALUES ('00000000-0000-0000-0000-000000000001', 'shadow', 'render', 1)$$,
    '23503', 'cost must belong to the job channel');
SELECT pg_temp.expect_error($$DELETE FROM job_costs$$, '42501', 'job_costs are append-only');

-- ------------------------------------------------------------ attribution and audit trail
SELECT pg_temp.check(
    (SELECT bool_and(actor = 'guard-test') FROM job_events
     WHERE job_id = '00000000-0000-0000-0000-000000000001'),
    'job_events record app.actor');
SELECT pg_temp.check(
    (SELECT count(*) = 11 FROM job_events
     WHERE job_id = '00000000-0000-0000-0000-000000000001'),
    'every transition of the published job is in job_events');
SELECT pg_temp.check(
    (SELECT count(DISTINCT action) = 6 FROM audit_events
     WHERE subject = 'job:00000000-0000-0000-0000-000000000001'
       AND action IN ('job.created', 'job.transition', 'approval.consumed', 'upload.pending',
                      'upload.uploaded', 'upload.live')),
    'audit trail covers creation, transitions, approval and upload lifecycle');
SELECT pg_temp.check(audit_chain_first_break() IS NULL, 'audit chain still verifies');

\echo 'publish schema guard tests passed'
ROLLBACK;
