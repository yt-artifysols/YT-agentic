-- Guard tests for 006_knowledge_inputs.sql and 007_agents_platform.sql: knowledge drafts and
-- human-only approval, immutable approved versions, job knowledge pins, owner inputs and
-- provenance, channel-bound memory, licenses, versioned assets, per-job budgets, alerts.
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

DO $$ BEGIN PERFORM set_config('app.actor', 'guard', true); END $$;

INSERT INTO channels (id, display_name, storage_prefix, memory_namespace, daily_budget_cents,
                      job_budget_cents)
VALUES ('kb-a', 'A', 'channels/kb-a/', 'mem:kb-a', 1000, 100),
       ('kb-b', 'B', 'channels/kb-b/', 'mem:kb-b', 1000, 100);
INSERT INTO hub_users (username, display_name, role)
VALUES ('kb-approver', 'Approver', 'approver'), ('kb-operator', 'Operator', 'operator');
INSERT INTO video_jobs (id, channel_id, origin)
VALUES ('00000000-0000-0000-0000-00000000aa01', 'kb-a', 'owner'),
       ('00000000-0000-0000-0000-00000000bb01', 'kb-b', 'owner');

-- ---------------------------------------------------------------- knowledge

SELECT pg_temp.expect_error(
    $$INSERT INTO knowledge_docs (channel_id, kind, version, content, content_sha256, status,
                                  proposed_by, approved_by, approved_at)
      VALUES ('kb-a', 'brand_voice', 1, 'x', 'sha256:' || repeat('0', 64), 'approved',
              'agent:script', 'agent:script', now())$$,
    '42501', 'knowledge cannot be inserted as approved');
SELECT pg_temp.expect_ok(
    $$INSERT INTO knowledge_docs (channel_id, kind, version, content, content_sha256, proposed_by)
      VALUES ('kb-a', 'banned_claims', 99, 'cures cancer', 'sha256:' || repeat('0', 64),
              'agent:research')$$,
    'an agent can submit a draft');
SELECT pg_temp.check(
    (SELECT version = 1 AND content_sha256 = 'sha256:' || encode(sha256('cures cancer'), 'hex')
     FROM knowledge_docs WHERE channel_id = 'kb-a' AND kind = 'banned_claims'),
    'version and sha256 are assigned by the database');
SELECT pg_temp.expect_error(
    $$UPDATE knowledge_docs SET status = 'approved', approved_by = 'agent:research',
             approved_at = now() WHERE channel_id = 'kb-a' AND kind = 'banned_claims'$$,
    '42501', 'an agent cannot approve knowledge');
SELECT pg_temp.expect_error(
    $$UPDATE knowledge_docs SET status = 'approved', approved_by = 'kb-operator',
             approved_at = now() WHERE channel_id = 'kb-a' AND kind = 'banned_claims'$$,
    '42501', 'an operator cannot approve knowledge');
SELECT pg_temp.expect_error(
    $$UPDATE knowledge_docs SET content = 'edited' WHERE channel_id = 'kb-a'$$,
    '42501', 'draft content is immutable');
SELECT pg_temp.expect_ok(
    $$UPDATE knowledge_docs SET status = 'approved', approved_by = 'kb-approver',
             approved_at = now() WHERE channel_id = 'kb-a' AND kind = 'banned_claims'$$,
    'a human approver approves knowledge');
SELECT pg_temp.check(
    EXISTS (SELECT 1 FROM audit_events WHERE action = 'knowledge.approved'
            AND payload->>'by' = 'kb-approver'),
    'knowledge approval is audited');
SELECT pg_temp.expect_error(
    $$UPDATE knowledge_docs SET status = 'rejected', rejected_by = 'kb-approver',
             rejected_at = now(), reject_reason = 'oops' WHERE channel_id = 'kb-a'$$,
    '42501', 'approved knowledge is immutable');
SELECT pg_temp.expect_error($$DELETE FROM knowledge_docs$$, '42501', 'knowledge is never deleted');

-- Two drafts on top of version 1: approving one makes the other stale.
INSERT INTO knowledge_docs (channel_id, kind, version, base_version, content, content_sha256,
                            proposed_by)
VALUES ('kb-a', 'banned_claims', 0, 1, 'v2 a', 'sha256:' || repeat('0', 64), 'agent:qa'),
       ('kb-a', 'banned_claims', 0, 1, 'v2 b', 'sha256:' || repeat('0', 64), 'kb-operator');
SELECT pg_temp.expect_ok(
    $$UPDATE knowledge_docs SET status = 'approved', approved_by = 'kb-approver',
             approved_at = now() WHERE content = 'v2 a'$$,
    'approve a draft based on the current version');
SELECT pg_temp.expect_error(
    $$UPDATE knowledge_docs SET status = 'approved', approved_by = 'kb-approver',
             approved_at = now() WHERE content = 'v2 b'$$,
    'YTK01', 'a stale draft cannot be approved');
SELECT pg_temp.expect_error(
    $$UPDATE knowledge_docs SET status = 'rejected', rejected_by = 'kb-approver',
             rejected_at = now(), reject_reason = '' WHERE content = 'v2 b'$$,
    '23514', 'a rejection needs a reason');
SELECT pg_temp.expect_ok(
    $$UPDATE knowledge_docs SET status = 'rejected', rejected_by = 'kb-approver',
             rejected_at = now(), reject_reason = 'superseded' WHERE content = 'v2 b'$$,
    'a human approver rejects a draft');
SELECT pg_temp.expect_error(
    $$INSERT INTO knowledge_docs (channel_id, kind, version, base_version, content,
                                  content_sha256, proposed_by)
      VALUES ('kb-a', 'banned_claims', 0, 3, 'x', 'sha256:' || repeat('0', 64), 'agent:qa')$$,
    '23514', 'a draft must be based on an approved version');

-- Job knowledge pins: approved only, same channel only.
SELECT pg_temp.expect_ok(
    $$INSERT INTO job_knowledge (job_id, channel_id, doc_id, kind, version)
      SELECT '00000000-0000-0000-0000-00000000aa01', 'kb-a', id, '', 0 FROM knowledge_docs
      WHERE content = 'v2 a'$$,
    'a job pins an approved knowledge version');
SELECT pg_temp.check(
    (SELECT kind = 'banned_claims' AND version = 2 FROM job_knowledge),
    'pinned kind and version come from the document');
SELECT pg_temp.expect_error(
    $$INSERT INTO job_knowledge (job_id, channel_id, doc_id, kind, version)
      SELECT '00000000-0000-0000-0000-00000000aa01', 'kb-a', id, '', 0 FROM knowledge_docs
      WHERE content = 'v2 b'$$,
    '42501', 'a job cannot use rejected or draft knowledge');
SELECT pg_temp.expect_error(
    $$INSERT INTO job_knowledge (job_id, channel_id, doc_id, kind, version)
      SELECT '00000000-0000-0000-0000-00000000bb01', 'kb-b', id, '', 0 FROM knowledge_docs
      WHERE content = 'v2 a'$$,
    '23503', 'a job cannot use another channel''s knowledge');
SELECT pg_temp.expect_error($$DELETE FROM job_knowledge$$, '42501', 'job knowledge is append-only');

-- ---------------------------------------------------------------- owner inputs

SELECT pg_temp.expect_error(
    $$INSERT INTO owner_inputs (channel_id, kind, body, sha256, submitted_by)
      VALUES ('kb-a', 'idea', 'An idea', 'sha256:' || repeat('0', 64), 'owner')$$,
    '23514', 'owner input sha256 must match its body');
INSERT INTO owner_inputs (id, channel_id, kind, body, sha256, submitted_by)
VALUES ('00000000-0000-0000-0000-0000000000a1', 'kb-a', 'idea', 'An idea',
        'sha256:' || encode(sha256('An idea'), 'hex'), 'owner'),
       ('00000000-0000-0000-0000-0000000000a2', 'kb-a', 'goal', 'cadence: 3/week',
        'sha256:' || encode(sha256('cadence: 3/week'), 'hex'), 'owner');
SELECT pg_temp.expect_error(
    $$INSERT INTO owner_inputs (channel_id, kind, body, sha256, submitted_by)
      VALUES ('kb-a', 'idea', 'An idea', 'sha256:' || encode(sha256('An idea'), 'hex'), 'owner')$$,
    '23505', 'the same input is queued once per channel');
SELECT pg_temp.expect_error(
    $$INSERT INTO owner_inputs (channel_id, kind, body, sha256, submitted_by)
      VALUES ('kb-a', 'footage', 'clip', 'sha256:' || encode(sha256('clip'), 'hex'), 'owner')$$,
    '23514', 'footage needs a uri');
SELECT pg_temp.expect_ok(
    $$INSERT INTO job_owner_inputs (job_id, channel_id, input_id)
      VALUES ('00000000-0000-0000-0000-00000000aa01', 'kb-a',
              '00000000-0000-0000-0000-0000000000a1'),
             ('00000000-0000-0000-0000-00000000aa01', 'kb-a',
              '00000000-0000-0000-0000-0000000000a2')$$,
    'a job links an idea and a goal');
SELECT pg_temp.check(
    (SELECT status = 'used' AND used_by_job = '00000000-0000-0000-0000-00000000aa01'
     FROM owner_inputs WHERE kind = 'idea')
    AND (SELECT status = 'new' FROM owner_inputs WHERE kind = 'goal'),
    'linking marks the idea used; the goal stays available');
SELECT pg_temp.expect_error(
    $$INSERT INTO job_owner_inputs (job_id, channel_id, input_id)
      VALUES ('00000000-0000-0000-0000-00000000bb01', 'kb-b',
              '00000000-0000-0000-0000-0000000000a2')$$,
    '23503', 'a job cannot use another channel''s input');
SELECT pg_temp.expect_error(
    $$UPDATE owner_inputs SET body = 'changed' WHERE kind = 'idea'$$,
    '23514', 'owner input content is immutable');
SELECT pg_temp.expect_error(
    $$UPDATE owner_inputs SET status = 'new', used_by_job = NULL WHERE kind = 'idea'$$,
    '23514', 'a used input cannot be re-queued');
SELECT pg_temp.expect_error($$DELETE FROM owner_inputs$$, '42501', 'owner inputs are never deleted');
SELECT pg_temp.expect_error($$DELETE FROM job_owner_inputs$$, '42501',
                            'input provenance is append-only');

-- ---------------------------------------------------------------- memory

SELECT pg_temp.expect_ok(
    $$INSERT INTO channel_memory (channel_id, namespace, kind, content, embedding_model,
                                  embedding)
      VALUES ('kb-a', 'mem:kb-a', 'fact', 'A fact', 'fake', '[1,0,0]')$$,
    'memory row in the channel''s own namespace');
SELECT pg_temp.expect_error(
    $$INSERT INTO channel_memory (channel_id, namespace, kind, content, embedding_model,
                                  embedding)
      VALUES ('kb-a', 'mem:kb-b', 'fact', 'Leak', 'fake', '[1,0,0]')$$,
    '23503', 'memory cannot be written into another channel''s namespace');

-- ---------------------------------------------------------------- licenses and assets

INSERT INTO rights_register (license_ref, channel_id, asset_kind, provider)
VALUES ('lic_music-a', 'kb-a', 'music', 'fake-stock'),
       ('lic_music-old', NULL, 'music', 'fake-stock');
UPDATE rights_register SET revoked_at = now() WHERE license_ref = 'lic_music-old';
SELECT pg_temp.expect_error(
    $$UPDATE rights_register SET provider = 'other' WHERE license_ref = 'lic_music-a'$$,
    '23514', 'licenses are immutable');
SELECT pg_temp.expect_error(
    $$INSERT INTO job_assets (job_id, channel_id, name, version, kind, sha256, media_type,
                              provider)
      VALUES ('00000000-0000-0000-0000-00000000aa01', 'kb-a', 'music', 0, 'music',
              'sha256:' || repeat('1', 64), 'audio/mpeg', 'fake')$$,
    'YTL01', 'music needs a license');
SELECT pg_temp.expect_error(
    $$INSERT INTO job_assets (job_id, channel_id, name, version, kind, sha256, media_type,
                              license_ref, provider)
      VALUES ('00000000-0000-0000-0000-00000000aa01', 'kb-a', 'music', 0, 'music',
              'sha256:' || repeat('1', 64), 'audio/mpeg', 'lic_music-old', 'fake')$$,
    'YTL01', 'a revoked license is refused');
SELECT pg_temp.expect_error(
    $$INSERT INTO job_assets (job_id, channel_id, name, version, kind, sha256, media_type,
                              license_ref, provider)
      VALUES ('00000000-0000-0000-0000-00000000bb01', 'kb-b', 'music', 0, 'music',
              'sha256:' || repeat('1', 64), 'audio/mpeg', 'lic_music-a', 'fake')$$,
    'YTL01', 'a license for another channel is refused');
SELECT pg_temp.expect_ok(
    $$INSERT INTO job_assets (job_id, channel_id, name, version, kind, sha256, media_type,
                              license_ref, provider)
      VALUES ('00000000-0000-0000-0000-00000000aa01', 'kb-a', 'music', 0, 'music',
              'sha256:' || repeat('1', 64), 'audio/mpeg', 'lic_music-a', 'fake'),
             ('00000000-0000-0000-0000-00000000aa01', 'kb-a', 'music', 0, 'music',
              'sha256:' || repeat('2', 64), 'audio/mpeg', 'lic_music-a', 'fake')$$,
    'licensed music stored twice');
SELECT pg_temp.check(
    (SELECT array_agg(version ORDER BY version) = ARRAY[1, 2] FROM job_assets),
    'asset versions are assigned by the database');
SELECT pg_temp.expect_error($$UPDATE job_assets SET sha256 = sha256$$, '42501',
                            'job assets are append-only');

-- ---------------------------------------------------------------- budgets and alerts

SELECT pg_temp.expect_ok(
    $$INSERT INTO job_costs (job_id, channel_id, kind, cost_cents, provider)
      VALUES ('00000000-0000-0000-0000-00000000aa01', 'kb-a', 'llm', 90, 'fake-llm')$$,
    'cost within the job budget');
SELECT pg_temp.expect_error(
    $$INSERT INTO job_costs (job_id, channel_id, kind, cost_cents, provider)
      VALUES ('00000000-0000-0000-0000-00000000aa01', 'kb-a', 'llm', 20, 'fake-llm')$$,
    'YTB02', 'the per-job budget is enforced');
SELECT pg_temp.expect_ok(
    $$INSERT INTO alerts (channel_id, job_id, kind, message)
      VALUES ('kb-a', '00000000-0000-0000-0000-00000000aa01', 'budget.job', 'cap hit')$$,
    'alert recorded');
SELECT pg_temp.check(
    EXISTS (SELECT 1 FROM audit_events WHERE action = 'alert.budget.job'),
    'alerts are audited');
SELECT pg_temp.expect_error($$DELETE FROM alerts$$, '42501', 'alerts are append-only');

SELECT pg_temp.check(audit_chain_first_break() IS NULL, 'audit chain still verifies');

ROLLBACK;
\echo 'knowledge schema guard tests passed'
