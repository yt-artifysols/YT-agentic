-- Guard tests for 015_serverless_driver.sql: workflow decisions are append-only, can only be
-- consumed, and the approval token is erased on consumption. Rolled back.
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

INSERT INTO clusters (id, display_name) VALUES ('sg-cl', 'sg');
INSERT INTO google_projects (id, daily_quota_units, oauth_client_ref)
    VALUES ('sg-proj', 1000, 'env://X');
INSERT INTO channels (id, display_name, storage_prefix, memory_namespace, cluster_id,
                      google_project_id, youtube_credentials_ref)
    VALUES ('sg-ch', 'sg', 'channels/sg-ch/', 'mem:sg-ch', 'sg-cl', 'sg-proj', 'env://T');
INSERT INTO video_jobs (id, channel_id, origin)
    VALUES ('00000000-0000-0000-0000-0000000000a1', 'sg-ch', 'owner');

-- an approve decision without a token is refused
SELECT pg_temp.expect_error(
    $$INSERT INTO workflow_decisions (job_id, action, actor, content_hash)
      VALUES ('00000000-0000-0000-0000-0000000000a1', 'approve', 'hub:x', 'h')$$,
    '23514', 'an approve decision needs a token');

INSERT INTO workflow_decisions (job_id, action, actor, content_hash, token)
    VALUES ('00000000-0000-0000-0000-0000000000a1', 'approve', 'hub:x', 'h', 'tok');

SELECT pg_temp.expect_error(
    $$UPDATE workflow_decisions SET actor = 'hub:y'$$, 'P0001', 'only consumed_at may change');
SELECT pg_temp.expect_error(
    $$DELETE FROM workflow_decisions$$, 'P0001', 'decisions are never deleted');

UPDATE workflow_decisions SET consumed_at = now();
DO $$
BEGIN
    IF (SELECT token FROM workflow_decisions) IS NOT NULL THEN
        RAISE EXCEPTION 'FAIL the token must be erased on consumption';
    END IF;
    RAISE NOTICE 'ok   the token is erased when the decision is consumed';
END $$;

SELECT pg_temp.expect_error(
    $$UPDATE workflow_decisions SET consumed_at = NULL$$, 'P0001', 'a consumed decision is final');

ROLLBACK;
\echo serverless schema guard tests passed
