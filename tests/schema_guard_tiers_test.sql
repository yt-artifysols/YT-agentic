-- Guard tests for 013_autonomy_tiers.sql: tier promotion needs an admin, demotion needs a reason. Rolled back.
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

INSERT INTO hub_users (username, display_name, role) VALUES
  ('boss', 'Boss', 'admin'), ('appr', 'Approver', 'approver');
INSERT INTO clusters (id, display_name, live_enabled) VALUES ('tc', 'Tier cluster', true);
INSERT INTO channels (id, display_name, storage_prefix, memory_namespace, cluster_id,
                      autonomy_tier)
VALUES ('t-a', 'A', 'channels/t-a/', 'mem:t-a', 'tc', 'gated'),
       ('t-s', 'S', 'channels/t-s/', 'mem:t-s', 'tc', 'shadow');

-- ------------------------------------------------------------ promotion
SELECT pg_temp.check((SELECT approval_tier FROM channels WHERE id = 't-a') = 1,
                     'channels start on approve-all');
SELECT pg_temp.expect_error(
    $$UPDATE channels SET approval_tier = 2 WHERE id = 't-a'$$, 'YTT01',
    'no promotion without a confirmed recommendation');
INSERT INTO tier_recommendations (channel_id, evidence) VALUES ('t-a', '{"days": 30}');
SELECT pg_temp.expect_error(
    $$UPDATE channels SET approval_tier = 2 WHERE id = 't-a'$$, 'YTT01',
    'a pending recommendation does not promote');
SELECT pg_temp.expect_error(
    $$UPDATE tier_recommendations SET status = 'confirmed', decided_by = 'appr',
      note = 'looks good to me' WHERE channel_id = 't-a'$$, 'YTT01',
    'an approver cannot confirm a promotion');
SELECT pg_temp.expect_error(
    $$UPDATE tier_recommendations SET status = 'confirmed', decided_by = 'boss', note = 'ok'
      WHERE channel_id = 't-a'$$, 'YTT01', 'a confirmation needs a real note');
SELECT pg_temp.expect_error(
    $$INSERT INTO tier_changes (channel_id, from_tier, to_tier, kind, reason)
      VALUES ('t-a', 1, 2, 'promotion', 'forged history')$$, 'YTT01',
    'tier history cannot be written directly');
SELECT pg_temp.expect_ok(
    $$UPDATE tier_recommendations SET status = 'confirmed', decided_by = 'boss',
      note = 'reviewed the first month myself' WHERE channel_id = 't-a'$$,
    'an admin confirms the recommendation');
SELECT pg_temp.expect_ok($$UPDATE channels SET approval_tier = 2 WHERE id = 't-a'$$,
                         'a confirmed recommendation promotes in the same transaction');
SELECT pg_temp.check((SELECT count(*) FROM tier_changes WHERE channel_id = 't-a'
                      AND kind = 'promotion' AND recommendation_id IS NOT NULL) = 1,
                     'the promotion is recorded with its recommendation');
SELECT pg_temp.expect_error(
    $$UPDATE tier_recommendations SET status = 'dismissed', decided_by = 'boss',
      note = 'changed my mind about it' WHERE channel_id = 't-a'$$, 'YTT01',
    'a decided recommendation cannot change');
SELECT pg_temp.expect_error($$DELETE FROM tier_recommendations$$, 'YTT01',
                            'recommendations cannot be deleted');
INSERT INTO tier_recommendations (channel_id, evidence) VALUES ('t-s', '{}');
UPDATE tier_recommendations SET status = 'confirmed', decided_by = 'boss',
       note = 'confirmed for the shadow test' WHERE channel_id = 't-s';
SELECT pg_temp.expect_error(
    $$UPDATE channels SET approval_tier = 2 WHERE id = 't-s'$$, 'YTT01',
    'a shadow channel cannot be promoted');

-- ------------------------------------------------------------ demotion
INSERT INTO tier_recommendations (channel_id, evidence) VALUES ('t-a', '{}');
SELECT pg_temp.expect_error(
    $$UPDATE channels SET approval_tier = 1 WHERE id = 't-a'$$, 'YTT01',
    'a demotion needs a recorded reason');
SELECT pg_temp.expect_ok(
    $$INSERT INTO channel_strikes (channel_id, kind, note)
      VALUES ('t-a', 'strike', 'community guidelines strike on video x')$$,
    'a strike is recorded');
SELECT pg_temp.check((SELECT approval_tier FROM channels WHERE id = 't-a') = 1,
                     'a strike returns the channel to approve-all');
SELECT pg_temp.check((SELECT count(*) FROM tier_changes WHERE channel_id = 't-a'
                      AND kind = 'demotion' AND reason LIKE 'strike recorded%') = 1,
                     'the demotion is recorded with its reason');
SELECT pg_temp.check((SELECT status FROM tier_recommendations WHERE channel_id = 't-a'
                      AND status = 'superseded') = 'superseded',
                     'a demotion supersedes a pending recommendation');
SELECT pg_temp.check((SELECT count(*) FROM alerts WHERE channel_id = 't-a'
                      AND kind = 'tier.demoted') = 1, 'operators are alerted');
SELECT pg_temp.expect_error($$UPDATE channel_strikes SET note = 'rewritten history here'$$,
                            '42501', 'strikes are append-only');
SELECT pg_temp.expect_error($$DELETE FROM tier_changes$$, '42501', 'tier history is append-only');
INSERT INTO video_jobs (id, channel_id, origin)
VALUES ('00000000-0000-0000-0000-0000000000a1', 't-a', 'owner');
INSERT INTO review_outcomes (job_id, channel_id, content_hash, decision, lane, low_risk, tier,
                             decided_by)
VALUES ('00000000-0000-0000-0000-0000000000a1', 't-a', 'sha256:x', 'approved', 'fast', true, 2,
        'boss');
SELECT pg_temp.expect_error($$UPDATE review_outcomes SET decision = 'rejected'$$, '42501',
                            'a recorded decision cannot be rewritten');
SELECT pg_temp.expect_error($$DELETE FROM review_outcomes$$, '42501',
                            'review outcomes are append-only');
SELECT pg_temp.check((SELECT count(*) FROM audit_events
                      WHERE action IN ('tier.promoted', 'tier.demoted', 'tier.strike')) >= 3,
                     'every tier change is in the audit trail');
SELECT pg_temp.check(audit_chain_first_break() IS NULL, 'audit chain still verifies');

ROLLBACK;
\echo 'tier schema guard tests passed'
