-- DB invariant guard tests. Run against a real Postgres after all migrations are applied:
--   psql -v ON_ERROR_STOP=1 -f tests/schema_guard_test.sql
-- Everything runs in one transaction that is rolled back, so the database is left untouched.
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

INSERT INTO channels (id, display_name, storage_prefix, memory_namespace) VALUES
    ('chan-a', 'Channel A', 'channels/chan-a/', 'mem:chan-a'),
    ('chan-b', 'Channel B', 'channels/chan-b/', 'mem:chan-b');

INSERT INTO video_jobs (id, channel_id, origin) VALUES
    ('00000000-0000-0000-0000-00000000000a', 'chan-a', 'owner'),
    ('00000000-0000-0000-0000-00000000000b', 'chan-b', 'trigger');

-- ------------------------------------------------------------ per-channel isolation
SELECT pg_temp.expect_error(
    $$INSERT INTO channels (id, display_name, storage_prefix, memory_namespace)
      VALUES ('chan-c', 'C', 'channels/chan-a/', 'mem:chan-c')$$,
    '23505', 'storage prefix is unique per channel');
SELECT pg_temp.expect_error(
    $$INSERT INTO channels (id, display_name, storage_prefix, memory_namespace)
      VALUES ('chan-c', 'C', 'channels/chan-c/', 'mem:chan-a')$$,
    '23505', 'memory namespace is unique per channel');

-- ------------------------------------------------------------ state machine
SELECT pg_temp.expect_error(
    $$INSERT INTO video_jobs (channel_id, origin, state) VALUES ('chan-a', 'owner', 'approved')$$,
    '23514', 'new jobs must start planned');
SELECT pg_temp.expect_error(
    $$INSERT INTO video_jobs (channel_id, origin) VALUES ('chan-a', 'derived')$$,
    '23514', 'derived jobs need a parent');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET state = 'publishing' WHERE id = '00000000-0000-0000-0000-00000000000a'$$,
    '23514', 'cannot skip from planned to publishing');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET channel_id = 'chan-b' WHERE id = '00000000-0000-0000-0000-00000000000a'$$,
    '23514', 'channel_id is immutable');
SELECT pg_temp.expect_error(
    $$DELETE FROM video_jobs WHERE id = '00000000-0000-0000-0000-00000000000a'$$,
    '42501', 'jobs cannot be deleted');

SELECT pg_temp.expect_ok($$UPDATE video_jobs SET state = 'researching'
    WHERE id = '00000000-0000-0000-0000-00000000000a'$$, 'planned -> researching');
SELECT pg_temp.expect_ok($$UPDATE video_jobs SET state = 'scripting'
    WHERE id = '00000000-0000-0000-0000-00000000000a'$$, 'researching -> scripting');
SELECT pg_temp.expect_ok($$UPDATE video_jobs SET state = 'rendering'
    WHERE id = '00000000-0000-0000-0000-00000000000a'$$, 'scripting -> rendering');
SELECT pg_temp.expect_ok($$UPDATE video_jobs SET state = 'qa'
    WHERE id = '00000000-0000-0000-0000-00000000000a'$$, 'rendering -> qa');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET state = 'awaiting_approval'
      WHERE id = '00000000-0000-0000-0000-00000000000a'$$,
    '23514', 'awaiting_approval requires content hash');
SELECT pg_temp.expect_ok($$UPDATE video_jobs SET state = 'awaiting_approval',
    content_hash = 'sha256:' || repeat('a', 64), manifest = '{"metadata": {}, "assets": {}}'
    WHERE id = '00000000-0000-0000-0000-00000000000a'$$, 'qa -> awaiting_approval');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET content_hash = 'sha256:' || repeat('b', 64)
      WHERE id = '00000000-0000-0000-0000-00000000000a'$$,
    '23514', 'content hash frozen while awaiting approval');

-- ------------------------------------------------------------ approval gate
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET state = 'approved' WHERE id = '00000000-0000-0000-0000-00000000000a'$$,
    '42501', 'approved requires an approval row');
SELECT pg_temp.expect_error(
    $$INSERT INTO approvals (job_id, channel_id, content_hash, token_jti, approver, expires_at)
      VALUES ('00000000-0000-0000-0000-00000000000a', 'chan-a', 'sha256:' || repeat('b', 64),
              'tok-wrong-hash', 'owner@example.test', now() + interval '1 hour')$$,
    '23514', 'approval must match job content hash');
SELECT pg_temp.expect_error(
    $$INSERT INTO approvals (job_id, channel_id, content_hash, token_jti, approver, expires_at)
      VALUES ('00000000-0000-0000-0000-00000000000a', 'chan-b', 'sha256:' || repeat('a', 64),
              'tok-wrong-channel', 'owner@example.test', now() + interval '1 hour')$$,
    '23503', 'approval must be bound to the job channel');
SELECT pg_temp.expect_error(
    $$INSERT INTO approvals (job_id, channel_id, content_hash, token_jti, approver, expires_at)
      VALUES ('00000000-0000-0000-0000-00000000000b', 'chan-b', 'sha256:' || repeat('a', 64),
              'tok-not-awaiting', 'owner@example.test', now() + interval '1 hour')$$,
    '23514', 'approval only for jobs awaiting approval');

-- An expired approval does not open the gate.
SELECT pg_temp.expect_ok(
    $$INSERT INTO approvals (job_id, channel_id, content_hash, token_jti, approver,
                             issued_at, expires_at)
      VALUES ('00000000-0000-0000-0000-00000000000a', 'chan-a', 'sha256:' || repeat('a', 64),
              'tok-expired', 'owner@example.test', now() - interval '2 hours',
              now() - interval '1 hour')$$, 'record expired approval');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET state = 'approved' WHERE id = '00000000-0000-0000-0000-00000000000a'$$,
    '42501', 'expired approval does not open the gate');

-- A revoked approval does not open the gate.
SELECT pg_temp.expect_ok(
    $$INSERT INTO approvals (job_id, channel_id, content_hash, token_jti, approver, expires_at)
      VALUES ('00000000-0000-0000-0000-00000000000a', 'chan-a', 'sha256:' || repeat('a', 64),
              'tok-revoked', 'owner@example.test', now() + interval '1 hour')$$,
    'record approval to revoke');
SELECT pg_temp.expect_ok($$UPDATE approvals SET revoked_at = now() WHERE token_jti = 'tok-revoked'$$,
    'revoke approval');
SELECT pg_temp.expect_error(
    $$UPDATE approvals SET revoked_at = NULL WHERE token_jti = 'tok-revoked'$$,
    '42501', 'revocation cannot be undone');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET state = 'approved' WHERE id = '00000000-0000-0000-0000-00000000000a'$$,
    '42501', 'revoked approval does not open the gate');

SELECT pg_temp.expect_ok(
    $$INSERT INTO approvals (job_id, channel_id, content_hash, token_jti, approver, expires_at)
      VALUES ('00000000-0000-0000-0000-00000000000a', 'chan-a', 'sha256:' || repeat('a', 64),
              'tok-valid', 'owner@example.test', now() + interval '1 hour')$$,
    'record valid approval');
SELECT pg_temp.expect_error(
    $$UPDATE approvals SET content_hash = 'sha256:' || repeat('c', 64) WHERE token_jti = 'tok-valid'$$,
    '42501', 'approval content hash is immutable');
SELECT pg_temp.expect_error(
    $$DELETE FROM approvals WHERE token_jti = 'tok-valid'$$, '42501', 'approvals cannot be deleted');
SELECT pg_temp.expect_ok($$UPDATE video_jobs SET state = 'approved'
    WHERE id = '00000000-0000-0000-0000-00000000000a'$$, 'awaiting_approval -> approved');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET state = 'publishing' WHERE id = '00000000-0000-0000-0000-00000000000a'$$,
    '23514', 'approved cannot skip scheduling');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET manifest = '{"metadata": {"x": 1}, "assets": {}}'
      WHERE id = '00000000-0000-0000-0000-00000000000a'$$,
    '23514', 'manifest frozen after approval');
-- Publish-side rules (scheduled, publishing, uploads) are covered in schema_guard_publish_test.sql.
SELECT pg_temp.expect_ok($$UPDATE video_jobs SET state = 'cancelled'
    WHERE id = '00000000-0000-0000-0000-00000000000a'$$, 'approved -> cancelled');
SELECT pg_temp.expect_error(
    $$UPDATE video_jobs SET state = 'planned' WHERE id = '00000000-0000-0000-0000-00000000000a'$$,
    '23514', 'terminal state is final');

-- ------------------------------------------------------------ job_events append-only
DO $$
BEGIN
    IF (SELECT count(*) FROM job_events
        WHERE job_id = '00000000-0000-0000-0000-00000000000a') <> 8 THEN
        RAISE EXCEPTION 'FAIL every transition is recorded in job_events';
    END IF;
    RAISE NOTICE 'ok   every transition is recorded in job_events';
END $$;
SELECT pg_temp.expect_error($$UPDATE job_events SET actor = 'x'$$, '42501',
    'job_events rows cannot be updated');
SELECT pg_temp.expect_error($$DELETE FROM job_events$$, '42501',
    'job_events rows cannot be deleted');
SELECT pg_temp.expect_error($$TRUNCATE job_events CASCADE$$, '42501',
    'job_events cannot be truncated');

-- ------------------------------------------------------------ audit hash chain
INSERT INTO audit_events (actor, action, subject, payload) VALUES
    ('approval-hub', 'approval.issued', 'job:a', '{"token": "tok-valid"}'),
    ('gateway', 'publish.started', 'job:a', '{}'),
    ('gateway', 'publish.done', 'job:a', '{"video": "x"}');
DO $$
BEGIN
    IF audit_chain_first_break() IS NOT NULL THEN
        RAISE EXCEPTION 'FAIL audit chain verifies';
    END IF;
    IF EXISTS (SELECT 1 FROM audit_events e
               WHERE e.id > (SELECT min(id) FROM audit_events) AND e.prev_hash IS NULL) THEN
        RAISE EXCEPTION 'FAIL audit rows are linked';
    END IF;
    RAISE NOTICE 'ok   audit chain verifies and is linked';
END $$;
SELECT pg_temp.expect_error($$UPDATE audit_events SET payload = '{}'$$, '42501',
    'audit_events rows cannot be updated');
SELECT pg_temp.expect_error($$DELETE FROM audit_events$$, '42501',
    'audit_events rows cannot be deleted');
SELECT pg_temp.expect_error($$TRUNCATE audit_events$$, '42501',
    'audit_events cannot be truncated');

-- Tampering (simulated with triggers disabled) is detected by the verifier.
ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only;
UPDATE audit_events SET payload = '{"video": "tampered"}'
    WHERE id = (SELECT max(id) FROM audit_events);
ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only;
DO $$
BEGIN
    IF audit_chain_first_break() IS DISTINCT FROM (SELECT max(id) FROM audit_events) THEN
        RAISE EXCEPTION 'FAIL tampering is detected';
    END IF;
    RAISE NOTICE 'ok   tampering is detected by audit_chain_first_break()';
END $$;

-- ------------------------------------------------------------ seed matches generator
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM job_state_transitions WHERE from_state IN
               (SELECT state FROM job_states WHERE is_terminal)) THEN
        RAISE EXCEPTION 'FAIL terminal states have no outgoing transitions';
    END IF;
    RAISE NOTICE 'ok   terminal states have no outgoing transitions';
END $$;

\echo 'schema guard tests passed'
ROLLBACK;
