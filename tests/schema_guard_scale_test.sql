-- Guard tests for 011_scale.sql: cluster rollout gate, sign-offs, fair-share pool tables. Rolled back.
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
INSERT INTO clusters (id, display_name, live_enabled) VALUES ('ra', 'Cluster A', true);
UPDATE clusters SET live_since = now() - interval '20 days' WHERE id = 'ra';

-- ---------------------------------------------------------------- rollout gate
SELECT pg_temp.expect_error(
    $$INSERT INTO clusters (id, display_name, requires_cluster, live_enabled)
      VALUES ('rb', 'Cluster B', 'ra', true)$$,
    'YTR01', 'a cluster cannot start live while its prerequisite has no sign-off');
SELECT pg_temp.expect_ok(
    $$INSERT INTO clusters (id, display_name, requires_cluster, live_enabled)
      VALUES ('rb', 'Cluster B', 'ra', false)$$, 'a dark cluster can be created');
INSERT INTO channels (id, display_name, storage_prefix, memory_namespace, cluster_id)
VALUES ('rb-1', 'B1', 'channels/rb-1/', 'mem:rb-1', 'rb');
SELECT pg_temp.expect_error($$UPDATE channels SET autonomy_tier = 'gated' WHERE id = 'rb-1'$$,
    'YTR01', 'channels of a dark cluster cannot be gated');
SELECT pg_temp.expect_error(
    $$INSERT INTO channels (id, display_name, storage_prefix, memory_namespace, cluster_id,
                            autonomy_tier)
      VALUES ('rb-2', 'B2', 'channels/rb-2/', 'mem:rb-2', 'rb', 'gated')$$,
    'YTR01', 'nor can they be created gated');
SELECT pg_temp.expect_error($$UPDATE clusters SET live_enabled = true WHERE id = 'rb'$$,
    'YTR01', 'enabling needs the prerequisite sign-off');

SELECT pg_temp.expect_error(
    $$INSERT INTO rollout_signoffs (cluster_id, reviewer, note)
      VALUES ('ra', 'appr', 'looks fine to me after reading the first weeks')$$,
    'YTR01', 'only an admin can sign a cluster off');
SELECT pg_temp.expect_error(
    $$INSERT INTO rollout_signoffs (cluster_id, reviewer, note)
      VALUES ('ra', 'ghost', 'looks fine to me after reading the first weeks')$$,
    'YTR01', 'the reviewer must be a real hub user');
SELECT pg_temp.expect_error(
    $$INSERT INTO rollout_signoffs (cluster_id, reviewer, note) VALUES ('ra', 'boss', 'ok')$$,
    '23514', 'a sign-off needs a real note');
SELECT pg_temp.expect_ok(
    $$INSERT INTO rollout_signoffs (cluster_id, reviewer, note, evidence)
      VALUES ('ra', 'boss', 'Reviewed the first two weeks of cluster A: stable.', '{"days": 20}')$$,
    'an admin signs the prerequisite off');
SELECT pg_temp.check(
    EXISTS (SELECT 1 FROM audit_events WHERE action = 'rollout.signoff'
            AND subject = 'cluster:ra'), 'sign-offs are audited');
SELECT pg_temp.expect_error($$UPDATE rollout_signoffs SET note = repeat('x', 30)$$, '42501',
    'sign-offs are append-only');
SELECT pg_temp.expect_error($$TRUNCATE rollout_signoffs$$, '42501', 'sign-offs cannot be truncated');

-- the sign-off must come AFTER the gate period, and the prerequisite must have run that long
UPDATE clusters SET live_since = now() - interval '5 days' WHERE id = 'ra';
SELECT pg_temp.expect_error($$UPDATE clusters SET live_enabled = true WHERE id = 'rb'$$,
    'YTR01', 'the prerequisite must have run live for the whole gate period');
UPDATE clusters SET live_since = now() - interval '20 days' WHERE id = 'ra';
DELETE FROM rollout_signoffs WHERE false;  -- (append-only; nothing to delete)
SET LOCAL session_replication_role = replica;
UPDATE rollout_signoffs SET created_at = now() - interval '10 days';  -- signed BEFORE the gate ended
SET LOCAL session_replication_role = origin;
SELECT pg_temp.expect_error($$UPDATE clusters SET live_enabled = true WHERE id = 'rb'$$,
    'YTR01', 'a sign-off given before the gate period ended does not count');
INSERT INTO rollout_signoffs (cluster_id, reviewer, note)
VALUES ('ra', 'boss', 'Re-reviewed after the full 14 day period: stable.');
SELECT pg_temp.expect_ok($$UPDATE clusters SET live_enabled = true WHERE id = 'rb'$$,
    'with the gate period run and a fresh sign-off, the cluster goes live');
SELECT pg_temp.check((SELECT live_since IS NOT NULL FROM clusters WHERE id = 'rb'),
    'live_since is stamped by the database');
SELECT pg_temp.check(EXISTS (SELECT 1 FROM audit_events WHERE action = 'rollout.enabled'
                             AND subject = 'cluster:rb'), 'enabling is audited');
SELECT pg_temp.expect_ok($$UPDATE channels SET autonomy_tier = 'gated' WHERE id = 'rb-1'$$,
    'now its channels may be gated');

-- publishing also needs a live cluster
UPDATE clusters SET live_enabled = false WHERE id = 'rb';
SELECT pg_temp.check(NOT channel_publishable('rb-1'), 'a dark cluster publishes nothing');
SELECT pg_temp.check(
    NOT (SELECT channel_publishable('rb-1')) , 'channel_publishable is false when not live');

-- ---------------------------------------------------------------- fair-share tables
SELECT pg_temp.expect_ok(
    $$INSERT INTO pool_limits (scope, scope_id, pool, max_concurrent)
      VALUES ('global', '', 'gpu_render', 2), ('cluster', 'ra', 'media', 3)$$, 'limits are stored');
SELECT pg_temp.expect_error(
    $$INSERT INTO pool_limits (scope, scope_id, pool, max_concurrent)
      VALUES ('global', 'x', 'qa', 2)$$, '23514', 'a global limit has no scope id');
SELECT pg_temp.expect_error(
    $$INSERT INTO pool_limits (scope, scope_id, pool, max_concurrent)
      VALUES ('cluster', 'ra', 'qa', 0)$$, '23514', 'a limit is at least 1');
SELECT pg_temp.expect_error(
    $$INSERT INTO pool_limits (scope, scope_id, pool, max_concurrent)
      VALUES ('cluster', 'ra', 'teleporter', 1)$$, '23514', 'only known pools');

-- ---------------------------------------------------------------- gate cannot be weakened
INSERT INTO clusters (id, display_name, requires_cluster, gate_days)
VALUES ('rg', 'Gated', 'ra', 14);
SELECT pg_temp.expect_error(
    $$UPDATE clusters SET requires_cluster = NULL WHERE id = 'rg'$$, 'YTR01',
    'a gate cannot be removed');
SELECT pg_temp.expect_error(
    $$UPDATE clusters SET gate_days = 1 WHERE id = 'rg'$$, 'YTR01', 'a gate cannot be shortened');
SELECT pg_temp.expect_ok(
    $$UPDATE clusters SET gate_days = 21 WHERE id = 'rg'$$, 'a gate can be lengthened');

SELECT pg_temp.check(audit_chain_first_break() IS NULL, 'audit chain still verifies');

ROLLBACK;
\echo 'scale schema guard tests passed'
