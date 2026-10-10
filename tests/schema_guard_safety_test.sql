-- Guard tests for 008_safety.sql: kill switches refuse approvals, risk assessments, content
-- fingerprints, the cluster circuit breaker tables. Runs in one transaction, rolled back.
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

DO $$ BEGIN PERFORM set_config('app.actor', 'guard', true); END $$;

INSERT INTO clusters (id, display_name) VALUES ('sf-clu', 'Safety cluster'), ('sf-clu2', 'Other');
INSERT INTO channels (id, display_name, storage_prefix, memory_namespace, cluster_id)
VALUES ('sf-a', 'A', 'channels/sf-a/', 'mem:sf-a', 'sf-clu'),
       ('sf-b', 'B', 'channels/sf-b/', 'mem:sf-b', 'sf-clu2'),
       ('sf-free', 'No cluster', 'channels/sf-free/', 'mem:sf-free', NULL);

CREATE FUNCTION pg_temp.awaiting(p_id uuid, p_channel text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO video_jobs (id, channel_id, origin) VALUES (p_id, p_channel, 'owner');
    UPDATE video_jobs SET state = 'researching' WHERE id = p_id;
    UPDATE video_jobs SET state = 'scripting' WHERE id = p_id;
    UPDATE video_jobs SET state = 'rendering' WHERE id = p_id;
    UPDATE video_jobs SET state = 'qa' WHERE id = p_id;
    UPDATE video_jobs SET state = 'awaiting_approval', content_hash = 'sha256:' || repeat('a', 64),
        manifest = '{"metadata": {"title": "t"}, "assets": {}}' WHERE id = p_id;
END;
$$;

SELECT pg_temp.awaiting('00000000-0000-0000-0000-0000000000f1', 'sf-a');
SELECT pg_temp.awaiting('00000000-0000-0000-0000-0000000000f2', 'sf-a');
SELECT pg_temp.awaiting('00000000-0000-0000-0000-0000000000f3', 'sf-free');

-- ---------------------------------------------------------------- kill switches vs approvals

CREATE FUNCTION pg_temp.approve(p_job uuid, p_channel text, p_jti text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO approvals (job_id, channel_id, content_hash, token_jti, approver, expires_at)
    VALUES (p_job, p_channel, 'sha256:' || repeat('a', 64), p_jti, 'owner@example.test',
            now() + interval '1 hour');
END;
$$;

UPDATE global_controls SET publishing_paused = true;
SELECT pg_temp.expect_error(
    $$SELECT pg_temp.approve('00000000-0000-0000-0000-0000000000f1', 'sf-a', 'jti-1')$$,
    'YTC01', 'the global kill switch refuses approvals');
UPDATE global_controls SET publishing_paused = false;

UPDATE clusters SET status = 'paused' WHERE id = 'sf-clu';
SELECT pg_temp.expect_error(
    $$SELECT pg_temp.approve('00000000-0000-0000-0000-0000000000f1', 'sf-a', 'jti-2')$$,
    'YTC01', 'a paused cluster refuses approvals');
SELECT pg_temp.expect_ok(
    $$SELECT pg_temp.approve('00000000-0000-0000-0000-0000000000f3', 'sf-free', 'jti-3')$$,
    'a channel without a cluster is not affected by cluster pauses');
UPDATE clusters SET status = 'active' WHERE id = 'sf-clu';

UPDATE channels SET status = 'paused' WHERE id = 'sf-a';
SELECT pg_temp.expect_error(
    $$SELECT pg_temp.approve('00000000-0000-0000-0000-0000000000f1', 'sf-a', 'jti-4')$$,
    'YTC01', 'a paused channel refuses approvals');
UPDATE channels SET status = 'active' WHERE id = 'sf-a';

SELECT pg_temp.expect_ok(
    $$SELECT pg_temp.approve('00000000-0000-0000-0000-0000000000f1', 'sf-a', 'jti-5')$$,
    'approvals work again after the kill switches are released');
SELECT pg_temp.check(channel_not_paused('sf-a') AND NOT channel_not_paused('nope'),
                     'channel_not_paused reflects the switches (unknown channel is not ok)');

-- ---------------------------------------------------------------- job risk

SELECT pg_temp.expect_ok(
    $$INSERT INTO job_risk (job_id, channel_id, score, escalate, blocked, reasons)
      VALUES ('00000000-0000-0000-0000-0000000000f2', 'sf-a', 0.42, true, false,
              '[{"code": "sensitive_topic.finance", "contribution": 0.2}]')$$,
    'a risk assessment is stored on the job');
SELECT pg_temp.check(
    EXISTS (SELECT 1 FROM audit_events WHERE action = 'risk.assessed'
            AND subject = 'job:00000000-0000-0000-0000-0000000000f2'),
    'risk assessments are audited');
SELECT pg_temp.expect_error(
    $$INSERT INTO job_risk (job_id, channel_id, score, escalate, blocked, reasons)
      VALUES ('00000000-0000-0000-0000-0000000000f2', 'sf-a', 1.5, true, false, '[]')$$,
    '23514', 'a risk score is within 0..1');
SELECT pg_temp.expect_error(
    $$INSERT INTO job_risk (job_id, channel_id, score, escalate, blocked, reasons)
      VALUES ('00000000-0000-0000-0000-0000000000f2', 'sf-b', 0.1, false, false, '[]')$$,
    '23503', 'risk cannot be attached to another channel''s job');
SELECT pg_temp.expect_error($$UPDATE job_risk SET score = 0$$, '42501', 'risk is append-only');
SELECT pg_temp.expect_error($$DELETE FROM job_risk$$, '42501', 'risk cannot be deleted');
SELECT pg_temp.expect_error($$TRUNCATE job_risk$$, '42501', 'risk cannot be truncated');

-- ---------------------------------------------------------------- content fingerprints

SELECT pg_temp.expect_ok(
    $$INSERT INTO content_fingerprints (job_id, channel_id, kind, key, minhash, embedding,
                                        embedding_model)
      VALUES ('00000000-0000-0000-0000-0000000000f2', 'sf-a', 'script', 'abc', ARRAY[1, 2, 3],
              '[1,0,0]', 'fake')$$,
    'script fingerprint stored');
SELECT pg_temp.expect_ok(
    $$INSERT INTO content_fingerprints (job_id, channel_id, kind, key, label)
      VALUES ('00000000-0000-0000-0000-0000000000f2', 'sf-a', 'media', 'sha', 'video')$$,
    'media fingerprint stored');
SELECT pg_temp.expect_error(
    $$INSERT INTO content_fingerprints (job_id, channel_id, kind, key, minhash, embedding,
                                        embedding_model)
      VALUES ('00000000-0000-0000-0000-0000000000f2', 'sf-a', 'script', 'abc', ARRAY[1],
              '[1,0,0]', 'fake')$$,
    '23505', 'the same fingerprint is stored once per job');
SELECT pg_temp.expect_error(
    $$INSERT INTO content_fingerprints (job_id, channel_id, kind, key)
      VALUES ('00000000-0000-0000-0000-0000000000f2', 'sf-a', 'script', 'no-signature')$$,
    '23514', 'a script fingerprint needs its signature and embedding');
SELECT pg_temp.expect_error(
    $$INSERT INTO content_fingerprints (job_id, channel_id, kind, key)
      VALUES ('00000000-0000-0000-0000-0000000000f2', 'sf-a', 'raw_text', 'x')$$,
    '23514', 'only known fingerprint kinds are stored');
SELECT pg_temp.expect_error(
    $$INSERT INTO content_fingerprints (job_id, channel_id, kind, key)
      VALUES ('00000000-0000-0000-0000-0000000000f2', 'sf-b', 'media', 'other')$$,
    '23503', 'a fingerprint cannot be attributed to another channel''s job');
SELECT pg_temp.expect_error(
    $$INSERT INTO content_fingerprints (job_id, channel_id, kind, key, minhash, embedding,
                                        embedding_model)
      VALUES ('00000000-0000-0000-0000-0000000000f2', 'sf-a', 'script', 'dims', ARRAY[1],
              '[1,0,0,0]', 'fake')$$,
    '23514', 'one embedding model cannot mix vector dimensions');
SELECT pg_temp.expect_error(
    $$INSERT INTO content_fingerprints (job_id, channel_id, kind, key, minhash, embedding,
                                        embedding_model)
      VALUES ('00000000-0000-0000-0000-0000000000f2', 'sf-a', 'script', 'zero', ARRAY[1],
              '[0,0,0]', 'other-model')$$,
    '23514', 'a zero embedding is refused');
SELECT pg_temp.expect_error($$UPDATE content_fingerprints SET key = 'x'$$, '42501',
                            'fingerprints are append-only');
SELECT pg_temp.expect_error($$DELETE FROM content_fingerprints$$, '42501',
                            'fingerprints cannot be deleted');
SELECT pg_temp.expect_error($$TRUNCATE content_fingerprints$$, '42501',
                            'fingerprints cannot be truncated');

-- ---------------------------------------------------------------- circuit breaker

SELECT pg_temp.expect_ok(
    $$INSERT INTO cluster_breaker_config (cluster_id, max_rejection_rate) VALUES ('sf-clu', 0.4)$$,
    'breaker thresholds are configurable per cluster');
SELECT pg_temp.check(
    EXISTS (SELECT 1 FROM audit_events WHERE action = 'breaker.config'
            AND subject = 'cluster:sf-clu'),
    'breaker configuration changes are audited');
SELECT pg_temp.expect_error(
    $$INSERT INTO cluster_breaker_config (cluster_id, max_rejection_rate) VALUES ('sf-clu2', 1.5)$$,
    '23514', 'a rejection rate is within 0..1');
SELECT pg_temp.expect_error(
    $$INSERT INTO cluster_breaker_config (cluster_id, max_cost_ratio) VALUES ('sf-clu2', 0.5)$$,
    '23514', 'a cost ratio below 1 would trip on normal spend');
SELECT pg_temp.expect_ok(
    $$INSERT INTO breaker_trips (cluster_id, reasons)
      VALUES ('sf-clu', '[{"code": "rejection_rate", "value": 0.8}]')$$,
    'a breaker trip is recorded');
SELECT pg_temp.check(
    EXISTS (SELECT 1 FROM audit_events WHERE action = 'breaker.tripped'),
    'breaker trips are audited');
SELECT pg_temp.expect_error(
    $$INSERT INTO breaker_trips (cluster_id, reasons) VALUES ('sf-clu', '[]')$$,
    '23514', 'a trip needs at least one reason');
SELECT pg_temp.expect_error($$UPDATE breaker_trips SET stats = '{}'$$, '42501',
                            'breaker trips are append-only');
SELECT pg_temp.expect_error($$DELETE FROM breaker_trips$$, '42501', 'trips cannot be deleted');
SELECT pg_temp.expect_error($$TRUNCATE breaker_trips$$, '42501', 'trips cannot be truncated');
SELECT pg_temp.expect_ok(
    $$INSERT INTO alerts (cluster_id, kind, message)
      VALUES ('sf-clu', 'breaker.tripped', 'rejection rate 80%')$$,
    'an alert can be about a cluster');
SELECT pg_temp.check(
    EXISTS (SELECT 1 FROM audit_events WHERE action = 'alert.breaker.tripped'
            AND subject = 'cluster:sf-clu'),
    'cluster alerts are audited against the cluster');

-- ---------------------------------------------------------------- fail closed

SELECT pg_temp.expect_error($$TRUNCATE global_controls$$, '42501',
                            'the global kill switch row cannot be truncated away');
CREATE TEMP TABLE saved_controls AS SELECT * FROM global_controls;
SET LOCAL session_replication_role = replica;  -- simulate tampering that bypasses triggers
DELETE FROM global_controls;
SET LOCAL session_replication_role = origin;
SELECT pg_temp.check(channel_not_paused('sf-free') IS NOT TRUE,
                     'a missing global_controls row counts as paused');
INSERT INTO global_controls SELECT * FROM saved_controls;

SELECT pg_temp.check(audit_chain_first_break() IS NULL, 'audit chain still verifies');

ROLLBACK;
\echo 'safety schema guard tests passed'
