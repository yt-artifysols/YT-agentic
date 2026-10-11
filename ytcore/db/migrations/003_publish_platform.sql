-- 003: publish platform. Clusters, Google projects with quota ledger, channel status and
-- autonomy tier, per-channel daily budget with a cost ledger, publish manifests, consumed
-- approval tokens (token_jti), uploads with idempotency keys, publish guards and an audit
-- trail for every transition, approval and upload.
BEGIN;

-- ---------------------------------------------------------------- actor

-- Callers run `SELECT set_config('app.actor', <actor>, true)` (equivalent to SET LOCAL) in the
-- same transaction as their writes; events and audit rows record it.
CREATE FUNCTION app_actor() RETURNS text LANGUAGE sql STABLE AS $$
    SELECT coalesce(nullif(current_setting('app.actor', true), ''), current_user::text);
$$;

ALTER TABLE job_events ALTER COLUMN actor SET DEFAULT app_actor();

-- ---------------------------------------------------------------- clusters, projects, channels

CREATE TABLE clusters (
    id           text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9_-]{1,62}$'),
    display_name text NOT NULL,
    status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused')),
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE google_projects (
    id                text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9-]{4,29}$'),
    daily_quota_units integer NOT NULL CHECK (daily_quota_units > 0),
    -- Reference to the OAuth client secret; never the secret itself.
    oauth_client_ref  text NOT NULL CHECK (oauth_client_ref ~ '^(vault|env)://[A-Za-z0-9_./#-]+$'),
    created_at        timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE channels
    ADD COLUMN cluster_id              text REFERENCES clusters (id),
    ADD COLUMN status                  text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused')),
    -- shadow: the full pipeline runs but nothing is ever published.
    -- gated:  publishing is allowed, always behind a human approval. There is no auto tier.
    ADD COLUMN autonomy_tier           text NOT NULL DEFAULT 'shadow'
        CHECK (autonomy_tier IN ('shadow', 'gated')),
    ADD COLUMN daily_budget_cents      integer NOT NULL DEFAULT 0 CHECK (daily_budget_cents >= 0),
    ADD COLUMN google_project_id       text REFERENCES google_projects (id),
    ADD COLUMN youtube_credentials_ref text
        CHECK (youtube_credentials_ref ~ '^(vault|env)://[A-Za-z0-9_./#-]+$');
CREATE INDEX channels_cluster_idx ON channels (cluster_id);

-- True when the channel may publish right now.
CREATE FUNCTION channel_publishable(p_channel text) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM channels c JOIN clusters k ON k.id = c.cluster_id
        WHERE c.id = p_channel
          AND c.status = 'active'
          AND k.status = 'active'
          AND c.autonomy_tier = 'gated'
          AND c.google_project_id IS NOT NULL
          AND c.youtube_credentials_ref IS NOT NULL);
$$;

-- ---------------------------------------------------------------- jobs

ALTER TABLE video_jobs
    ADD COLUMN manifest          jsonb,
    ADD COLUMN render_cost_cents integer NOT NULL DEFAULT 0 CHECK (render_cost_cents >= 0);

ALTER TABLE approvals RENAME COLUMN token_id TO token_jti;
ALTER TABLE approvals RENAME CONSTRAINT approvals_token_id_key TO approvals_token_jti_key;

-- Replaces the 001 guard: same rules and error codes, but content (hash and manifest) is frozen
-- from awaiting_approval onwards for every later state, including ones added after 001.
CREATE OR REPLACE FUNCTION video_jobs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    gated boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'planned' THEN
            RAISE EXCEPTION 'new jobs must start in state planned, got %', NEW.state
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.id <> OLD.id OR NEW.channel_id <> OLD.channel_id OR NEW.origin <> OLD.origin
       OR NEW.parent_job_id IS DISTINCT FROM OLD.parent_job_id THEN
        RAISE EXCEPTION 'job identity columns are immutable' USING ERRCODE = 'check_violation';
    END IF;

    IF (NEW.content_hash IS DISTINCT FROM OLD.content_hash
        OR NEW.manifest IS DISTINCT FROM OLD.manifest)
       AND OLD.state NOT IN ('planned', 'researching', 'scripting', 'rendering', 'qa') THEN
        RAISE EXCEPTION 'content_hash is frozen in state %', OLD.state
            USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.state IS DISTINCT FROM OLD.state THEN
        IF NOT EXISTS (SELECT 1 FROM job_state_transitions
                       WHERE from_state = OLD.state AND to_state = NEW.state) THEN
            RAISE EXCEPTION 'illegal job transition % -> %', OLD.state, NEW.state
                USING ERRCODE = 'check_violation';
        END IF;

        IF NEW.state = 'awaiting_approval' AND NEW.content_hash IS NULL THEN
            RAISE EXCEPTION 'awaiting_approval requires a content_hash'
                USING ERRCODE = 'check_violation';
        END IF;

        SELECT requires_approval INTO gated FROM job_states WHERE state = NEW.state;
        IF gated AND NOT EXISTS (
            SELECT 1 FROM approvals a
            WHERE a.job_id = NEW.id
              AND a.channel_id = NEW.channel_id
              AND a.content_hash = NEW.content_hash
              AND a.revoked_at IS NULL
              AND a.expires_at > now()
        ) THEN
            RAISE EXCEPTION 'approval gate: no valid approval for job % with hash %',
                NEW.id, NEW.content_hash USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;

    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

-- Publish-side rules, on top of video_jobs_guard (triggers fire in name order).
CREATE FUNCTION video_jobs_publish_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state IS NOT DISTINCT FROM OLD.state THEN
        RETURN NEW;
    END IF;
    IF NEW.state = 'awaiting_approval' AND NEW.manifest IS NULL THEN
        RAISE EXCEPTION 'awaiting_approval requires a manifest' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.state IN ('scheduled', 'publishing') AND NOT channel_publishable(NEW.channel_id) THEN
        RAISE EXCEPTION 'channel % is not publishable (paused, paused cluster, shadow tier or '
                        'missing credentials)', NEW.channel_id USING ERRCODE = 'YTC01';
    END IF;
    IF NEW.state = 'published' AND NOT EXISTS (
        SELECT 1 FROM uploads u WHERE u.job_id = NEW.id AND u.status = 'uploaded'
          AND u.content_hash = NEW.content_hash) THEN
        RAISE EXCEPTION 'published requires a completed upload' USING ERRCODE = 'YTU01';
    END IF;
    IF NEW.state = 'live' AND NOT EXISTS (
        SELECT 1 FROM uploads u WHERE u.job_id = NEW.id AND u.status = 'live'
          AND u.content_hash = NEW.content_hash) THEN
        RAISE EXCEPTION 'live requires an upload verified live' USING ERRCODE = 'YTU01';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER video_jobs_publish_guard
    BEFORE UPDATE ON video_jobs
    FOR EACH ROW EXECUTE FUNCTION video_jobs_publish_guard();

-- ---------------------------------------------------------------- audit trail

CREATE FUNCTION audit(p_action text, p_subject text, p_payload jsonb) RETURNS void
LANGUAGE sql AS $$
    INSERT INTO audit_events (actor, action, subject, payload, hash)
    VALUES (app_actor(), p_action, p_subject, p_payload, '\x'::bytea);
$$;

CREATE OR REPLACE FUNCTION video_jobs_record_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO job_events (job_id, from_state, to_state, actor)
        VALUES (NEW.id, NULL, NEW.state, app_actor());
        PERFORM audit('job.created', 'job:' || NEW.id,
                      jsonb_build_object('channel', NEW.channel_id, 'origin', NEW.origin));
    ELSIF NEW.state IS DISTINCT FROM OLD.state THEN
        INSERT INTO job_events (job_id, from_state, to_state, actor)
        VALUES (NEW.id, OLD.state, NEW.state, app_actor());
        PERFORM audit('job.transition', 'job:' || NEW.id,
                      jsonb_build_object('from', OLD.state, 'to', NEW.state,
                                         'content_hash', NEW.content_hash));
    END IF;
    RETURN NULL;
END;
$$;

CREATE FUNCTION approvals_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM audit('approval.consumed', 'job:' || NEW.job_id,
                      jsonb_build_object('jti', NEW.token_jti, 'approver', NEW.approver,
                                         'content_hash', NEW.content_hash));
    ELSE
        PERFORM audit('approval.revoked', 'job:' || NEW.job_id,
                      jsonb_build_object('jti', NEW.token_jti));
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER approvals_audit
    AFTER INSERT OR UPDATE ON approvals
    FOR EACH ROW EXECUTE FUNCTION approvals_audit();

-- ---------------------------------------------------------------- cost ledger and budget

CREATE TABLE job_costs (
    id         bigserial PRIMARY KEY,
    job_id     uuid NOT NULL,
    channel_id text NOT NULL,
    kind       text NOT NULL CHECK (kind IN ('render', 'llm', 'media', 'other')),
    cost_cents integer NOT NULL CHECK (cost_cents > 0),
    cost_date  date NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::date,
    actor      text NOT NULL DEFAULT app_actor(),
    at         timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (job_id, channel_id) REFERENCES video_jobs (id, channel_id)
);
CREATE INDEX job_costs_channel_day_idx ON job_costs (channel_id, cost_date);
CREATE INDEX job_costs_job_idx ON job_costs (job_id);

CREATE FUNCTION job_costs_budget() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    budget integer;
    spent  bigint;
BEGIN
    -- Lock the channel row so concurrent cost inserts for one channel serialize.
    SELECT daily_budget_cents INTO budget FROM channels WHERE id = NEW.channel_id FOR UPDATE;
    SELECT coalesce(sum(cost_cents), 0) INTO spent FROM job_costs
        WHERE channel_id = NEW.channel_id AND cost_date = NEW.cost_date;
    IF spent + NEW.cost_cents > budget THEN
        RAISE EXCEPTION 'daily budget exceeded for channel % (% + % > % cents)',
            NEW.channel_id, spent, NEW.cost_cents, budget USING ERRCODE = 'YTB01';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION job_costs_apply() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.kind = 'render' THEN
        UPDATE video_jobs SET render_cost_cents = render_cost_cents + NEW.cost_cents
        WHERE id = NEW.job_id;
    END IF;
    PERFORM audit('cost.recorded', 'job:' || NEW.job_id,
                  jsonb_build_object('kind', NEW.kind, 'cents', NEW.cost_cents));
    RETURN NULL;
END;
$$;

CREATE TRIGGER job_costs_budget BEFORE INSERT ON job_costs
    FOR EACH ROW EXECUTE FUNCTION job_costs_budget();
CREATE TRIGGER job_costs_apply AFTER INSERT ON job_costs
    FOR EACH ROW EXECUTE FUNCTION job_costs_apply();
CREATE TRIGGER job_costs_append_only BEFORE UPDATE OR DELETE ON job_costs
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER job_costs_no_truncate BEFORE TRUNCATE ON job_costs
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- quota ledger

-- YouTube Data API quota resets at midnight Pacific time.
CREATE FUNCTION youtube_quota_day() RETURNS date LANGUAGE sql STABLE AS $$
    SELECT (now() AT TIME ZONE 'America/Los_Angeles')::date;
$$;

CREATE TABLE quota_ledger (
    id                bigserial PRIMARY KEY,
    google_project_id text NOT NULL REFERENCES google_projects (id),
    usage_date        date NOT NULL DEFAULT youtube_quota_day(),
    units             integer NOT NULL CHECK (units > 0),
    operation         text NOT NULL CHECK (length(operation) > 0),
    job_id            uuid REFERENCES video_jobs (id),
    actor             text NOT NULL DEFAULT app_actor(),
    at                timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX quota_ledger_project_day_idx ON quota_ledger (google_project_id, usage_date);

CREATE FUNCTION quota_ledger_check() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    quota integer;
    used  bigint;
BEGIN
    SELECT daily_quota_units INTO quota FROM google_projects
        WHERE id = NEW.google_project_id FOR UPDATE;
    SELECT coalesce(sum(units), 0) INTO used FROM quota_ledger
        WHERE google_project_id = NEW.google_project_id AND usage_date = NEW.usage_date;
    IF used + NEW.units > quota THEN
        RAISE EXCEPTION 'YouTube quota exhausted for project % (% + % > %)',
            NEW.google_project_id, used, NEW.units, quota USING ERRCODE = 'YTQ01';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER quota_ledger_check BEFORE INSERT ON quota_ledger
    FOR EACH ROW EXECUTE FUNCTION quota_ledger_check();
CREATE TRIGGER quota_ledger_append_only BEFORE UPDATE OR DELETE ON quota_ledger
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER quota_ledger_no_truncate BEFORE TRUNCATE ON quota_ledger
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- uploads

CREATE TABLE uploads (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id               uuid NOT NULL,
    channel_id           text NOT NULL,
    idempotency_key      text NOT NULL UNIQUE CHECK (idempotency_key ~ '^upl_[0-9a-f]{64}$'),
    content_hash         text NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    approval_jti         text NOT NULL REFERENCES approvals (token_jti),
    google_project_id    text NOT NULL REFERENCES google_projects (id),
    synthetic_disclosure boolean NOT NULL,
    status               text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'uploaded', 'live', 'failed')),
    youtube_video_id     text UNIQUE,
    failure_reason       text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (job_id, channel_id) REFERENCES video_jobs (id, channel_id),
    CHECK (status IN ('pending', 'failed') OR youtube_video_id IS NOT NULL)
);
-- At most one upload in flight or done per job; failed attempts may be retried.
CREATE UNIQUE INDEX uploads_one_active_per_job ON uploads (job_id) WHERE status <> 'failed';
CREATE INDEX uploads_channel_created_idx ON uploads (channel_id, created_at);
CREATE INDEX uploads_status_idx ON uploads (status) WHERE status IN ('pending', 'uploaded');

CREATE FUNCTION uploads_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    j video_jobs;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT * INTO j FROM video_jobs WHERE id = NEW.job_id FOR UPDATE;
        IF j.state <> 'publishing' THEN
            RAISE EXCEPTION 'uploads require the job to be publishing (job is %)', j.state
                USING ERRCODE = 'YTU01';
        END IF;
        IF NEW.content_hash IS DISTINCT FROM j.content_hash THEN
            RAISE EXCEPTION 'upload content_hash does not match the job' USING ERRCODE = 'YTU01';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM approvals a
            WHERE a.token_jti = NEW.approval_jti AND a.job_id = NEW.job_id
              AND a.channel_id = NEW.channel_id AND a.content_hash = NEW.content_hash
              AND a.revoked_at IS NULL AND a.expires_at > now()) THEN
            RAISE EXCEPTION 'upload requires a valid approval for this job and content'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NOT channel_publishable(NEW.channel_id) THEN
            RAISE EXCEPTION 'channel % is not publishable', NEW.channel_id
                USING ERRCODE = 'YTC01';
        END IF;
        IF NEW.google_project_id IS DISTINCT FROM
           (SELECT google_project_id FROM channels WHERE id = NEW.channel_id) THEN
            RAISE EXCEPTION 'upload must use the channel''s Google project'
                USING ERRCODE = 'YTU01';
        END IF;
        IF NEW.status <> 'pending' OR NEW.youtube_video_id IS NOT NULL THEN
            RAISE EXCEPTION 'uploads start pending without a video id' USING ERRCODE = 'YTU01';
        END IF;
        RETURN NEW;
    END IF;

    IF (to_jsonb(NEW) - ARRAY['status', 'youtube_video_id', 'failure_reason', 'updated_at'])
       <> (to_jsonb(OLD) - ARRAY['status', 'youtube_video_id', 'failure_reason', 'updated_at'])
    THEN
        RAISE EXCEPTION 'upload identity columns are immutable' USING ERRCODE = 'YTU01';
    END IF;
    IF OLD.youtube_video_id IS NOT NULL
       AND NEW.youtube_video_id IS DISTINCT FROM OLD.youtube_video_id THEN
        RAISE EXCEPTION 'youtube_video_id is write-once' USING ERRCODE = 'YTU01';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'pending' AND NEW.status IN ('uploaded', 'failed')) OR
        (OLD.status = 'uploaded' AND NEW.status IN ('live', 'failed'))) THEN
        RAISE EXCEPTION 'illegal upload transition % -> %', OLD.status, NEW.status
            USING ERRCODE = 'YTU01';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE FUNCTION uploads_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.status IS DISTINCT FROM OLD.status THEN
        PERFORM audit('upload.' || NEW.status, 'job:' || NEW.job_id,
                      jsonb_build_object('upload', NEW.id, 'key', NEW.idempotency_key,
                                         'video', NEW.youtube_video_id,
                                         'synthetic_disclosure', NEW.synthetic_disclosure));
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER uploads_guard BEFORE INSERT OR UPDATE ON uploads
    FOR EACH ROW EXECUTE FUNCTION uploads_guard();
CREATE TRIGGER uploads_audit AFTER INSERT OR UPDATE ON uploads
    FOR EACH ROW EXECUTE FUNCTION uploads_audit();
CREATE TRIGGER uploads_no_delete BEFORE DELETE ON uploads
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER uploads_no_truncate BEFORE TRUNCATE ON uploads
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

COMMIT;
