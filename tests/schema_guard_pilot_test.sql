-- Guard tests for 010_pilot.sql: time zones, daily plans, metrics, scorecards. Rolled back.
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

INSERT INTO clusters (id, display_name) VALUES ('pl-clu', 'Pilot cluster');
INSERT INTO channels (id, display_name, storage_prefix, memory_namespace, cluster_id)
VALUES ('pl-a', 'A', 'channels/pl-a/', 'mem:pl-a', 'pl-clu'),
       ('pl-b', 'B', 'channels/pl-b/', 'mem:pl-b', 'pl-clu');
INSERT INTO video_jobs (id, channel_id, origin) VALUES
  ('00000000-0000-0000-0000-0000000000a1', 'pl-a', 'planner'),
  ('00000000-0000-0000-0000-0000000000a2', 'pl-a', 'planner'),
  ('00000000-0000-0000-0000-0000000000b1', 'pl-b', 'planner');

-- ---------------------------------------------------------------- schedule settings
SELECT pg_temp.expect_ok($$UPDATE channels SET audience_timezone = 'Asia/Tokyo' WHERE id = 'pl-a'$$,
                         'a real IANA time zone is accepted');
SELECT pg_temp.expect_error($$UPDATE channels SET audience_timezone = 'Mars/Olympus' WHERE id = 'pl-a'$$,
                            '23514', 'an unknown time zone is refused');
SELECT pg_temp.expect_error($$UPDATE channels SET publish_hour_local = 24 WHERE id = 'pl-a'$$,
                            '23514', 'publish hour must be 0..23');
SELECT pg_temp.expect_error($$UPDATE channels SET approval_lead_minutes = 1 WHERE id = 'pl-a'$$,
                            '23514', 'the approval lead time has a floor');
SELECT pg_temp.expect_error($$UPDATE clusters SET slot_spacing_minutes = -1 WHERE id = 'pl-clu'$$,
                            '23514', 'slot spacing cannot be negative');
SELECT pg_temp.expect_error(
    $$UPDATE channels SET analytics_credentials_ref = 'hunter2' WHERE id = 'pl-a'$$,
    '23514', 'the analytics token must be a secret reference, never a value');

-- ---------------------------------------------------------------- daily plans
SELECT pg_temp.expect_ok(
    $$INSERT INTO daily_plans (channel_id, plan_date, job_id, slot_at, slot_date,
                               approval_deadline, budget_cents)
      VALUES ('pl-a', '2026-05-05', '00000000-0000-0000-0000-0000000000a1',
              '2026-05-05 17:00+00', '2026-05-05', '2026-05-05 16:00+00', 500)$$,
    'a plan is created');
SELECT pg_temp.check(
    EXISTS (SELECT 1 FROM audit_events WHERE action = 'plan.created'
            AND subject = 'job:00000000-0000-0000-0000-0000000000a1'),
    'plan creation is audited');
SELECT pg_temp.expect_error(
    $$INSERT INTO daily_plans (channel_id, plan_date, job_id, slot_at, slot_date,
                               approval_deadline, budget_cents)
      VALUES ('pl-a', '2026-05-05', '00000000-0000-0000-0000-0000000000a2',
              '2026-05-05 17:00+00', '2026-05-05', '2026-05-05 16:00+00', 500)$$,
    '23505', 'one plan per channel, day and attempt (re-running never duplicates)');
SELECT pg_temp.expect_error(
    $$INSERT INTO daily_plans (channel_id, plan_date, attempt, job_id, slot_at, slot_date,
                               approval_deadline, budget_cents)
      VALUES ('pl-a', '2026-05-04', 2, '00000000-0000-0000-0000-0000000000a2',
              '2026-05-05 18:00+00', '2026-05-05', '2026-05-05 17:00+00', 500)$$,
    '23505', 'at most one plan per channel per publish day');
SELECT pg_temp.expect_error(
    $$INSERT INTO daily_plans (channel_id, plan_date, job_id, slot_at, slot_date,
                               approval_deadline, budget_cents)
      VALUES ('pl-b', '2026-05-05', '00000000-0000-0000-0000-0000000000a2',
              '2026-05-05 17:00+00', '2026-05-05', '2026-05-05 16:00+00', 500)$$,
    '23503', 'a plan cannot point at another channel''s job');
SELECT pg_temp.expect_error(
    $$INSERT INTO daily_plans (channel_id, plan_date, job_id, slot_at, slot_date,
                               approval_deadline, budget_cents)
      VALUES ('pl-b', '2026-05-05', '00000000-0000-0000-0000-0000000000b1',
              '2026-05-05 17:00+00', '2026-05-05', '2026-05-05 18:00+00', 500)$$,
    '23514', 'the approval deadline cannot be after the slot');
SELECT pg_temp.expect_error(
    $$UPDATE daily_plans SET slot_at = slot_at + interval '1 day'$$, '42501',
    'a plan''s slot is immutable (a replan is a new attempt)');
SELECT pg_temp.expect_ok($$UPDATE daily_plans SET status = 'closed'$$, 'a plan can be closed');
SELECT pg_temp.check(
    EXISTS (SELECT 1 FROM audit_events WHERE action = 'plan.closed'),
    'plan status changes are audited');
SELECT pg_temp.expect_error(
    $$INSERT INTO daily_plans (channel_id, plan_date, attempt, job_id, slot_at, slot_date,
                               approval_deadline, budget_cents)
      VALUES ('pl-a', '2026-05-04', 2, '00000000-0000-0000-0000-0000000000a2',
              '2026-05-05 18:00+00', '2026-05-05', '2026-05-05 17:00+00', 500)$$,
    '23505', 'a finished plan still occupies its day (no second video that day)');
SELECT pg_temp.expect_ok($$UPDATE daily_plans SET status = 'replanned'$$,
                         'a plan can be marked replaced');
SELECT pg_temp.expect_ok(
    $$INSERT INTO daily_plans (channel_id, plan_date, attempt, job_id, slot_at, slot_date,
                               approval_deadline, budget_cents)
      VALUES ('pl-a', '2026-05-04', 2, '00000000-0000-0000-0000-0000000000a2',
              '2026-05-05 18:00+00', '2026-05-05', '2026-05-05 17:00+00', 500)$$,
    'only a replaced plan frees its day for a replan');
SELECT pg_temp.expect_error($$DELETE FROM daily_plans$$, '42501', 'plans cannot be deleted');
SELECT pg_temp.expect_error($$TRUNCATE daily_plans$$, '42501', 'plans cannot be truncated');

-- ---------------------------------------------------------------- analytics
SELECT pg_temp.expect_ok(
    $$INSERT INTO video_metrics (channel_id, job_id, youtube_video_id, metric_date, views, ctr,
                                 retention)
      VALUES ('pl-a', '00000000-0000-0000-0000-0000000000a1', 'vid1', '2026-05-05', 10, 0.05, 0.4)$$,
    'metrics are stored');
SELECT pg_temp.expect_error(
    $$INSERT INTO video_metrics (channel_id, job_id, youtube_video_id, metric_date, views)
      VALUES ('pl-a', '00000000-0000-0000-0000-0000000000a1', 'vid1', '2026-05-05', 11)$$,
    '23505', 'one row per video and day');
SELECT pg_temp.expect_error(
    $$INSERT INTO video_metrics (channel_id, job_id, youtube_video_id, metric_date, views)
      VALUES ('pl-b', '00000000-0000-0000-0000-0000000000a1', 'vid2', '2026-05-05', 1)$$,
    '23503', 'metrics cannot be attributed to another channel''s job');
SELECT pg_temp.expect_error(
    $$INSERT INTO video_metrics (channel_id, job_id, youtube_video_id, metric_date, views, ctr)
      VALUES ('pl-a', '00000000-0000-0000-0000-0000000000a1', 'vid3', '2026-05-05', 1, 1.5)$$,
    '23514', 'CTR is a ratio');
SELECT pg_temp.check(
    EXISTS (SELECT 1 FROM audit_events WHERE action = 'metrics.ingested'), 'metrics are audited');
SELECT pg_temp.expect_error($$DELETE FROM video_metrics$$, '42501', 'metrics cannot be deleted');
SELECT pg_temp.expect_ok(
    $$INSERT INTO daily_scorecards (channel_id, score_date, jobs_planned, jobs_approved,
                                    jobs_rejected, policy_flags, videos_published, cost_cents, views)
      VALUES ('pl-a', '2026-05-05', 1, 1, 0, 0, 1, 100, 10)$$,
    'a scorecard is stored');
SELECT pg_temp.expect_error($$DELETE FROM daily_scorecards$$, '42501',
                            'scorecards cannot be deleted');
SELECT pg_temp.expect_ok(
    $$INSERT INTO performance_notes (youtube_video_id, iso_week, channel_id, verdict)
      VALUES ('vid1', '2026-W19', 'pl-a', 'above')$$, 'a learning is recorded once');
SELECT pg_temp.expect_error(
    $$INSERT INTO performance_notes (youtube_video_id, iso_week, channel_id, verdict)
      VALUES ('vid1', '2026-W19', 'pl-a', 'below')$$, '23505',
    'at most one learning per video per week');

-- ---------------------------------------------------------------- late approvals
INSERT INTO video_jobs (id, channel_id, origin, approval_deadline)
VALUES ('00000000-0000-0000-0000-0000000000c1', 'pl-a', 'planner', now() - interval '1 minute');
SELECT pg_temp.expect_error(
    $$INSERT INTO approvals (job_id, channel_id, content_hash, token_jti, approver, expires_at)
      VALUES ('00000000-0000-0000-0000-0000000000c1', 'pl-a', 'sha256:' || repeat('a', 64),
              'late-jti', 'owner@example.test', now() + interval '1 hour')$$,
    'YTD01', 'no approval is recorded after the approval deadline');
SELECT pg_temp.expect_error(
    $$UPDATE daily_plans SET status = 'active' WHERE status = 'replanned'$$, '42501',
    'a replaced plan stays replaced');
SELECT pg_temp.expect_error($$TRUNCATE video_metrics$$, '42501', 'metrics cannot be truncated');
SELECT pg_temp.expect_error($$DELETE FROM performance_notes$$, '42501', 'learnings are kept');

SELECT pg_temp.check(audit_chain_first_break() IS NULL, 'audit chain still verifies');

ROLLBACK;
\echo 'pilot schema guard tests passed'
