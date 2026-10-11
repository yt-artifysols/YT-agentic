-- 001: core schema. DB-enforced job state machine, approval gate,
-- append-only job_events and hash-chained, append-only audit_events.
-- Applied migrations are immutable: add a new numbered file instead of editing this one.
BEGIN;

CREATE TABLE channels (
    id               text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9_-]{1,62}$'),
    display_name     text NOT NULL,
    -- Per-channel isolation: each channel owns a distinct storage prefix and memory namespace.
    storage_prefix   text NOT NULL UNIQUE,
    memory_namespace text NOT NULL UNIQUE,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE job_states (
    state             text PRIMARY KEY,
    is_terminal       boolean NOT NULL,
    requires_approval boolean NOT NULL
);

CREATE TABLE job_state_transitions (
    from_state text NOT NULL REFERENCES job_states (state),
    to_state   text NOT NULL REFERENCES job_states (state),
    PRIMARY KEY (from_state, to_state)
);

CREATE TABLE video_jobs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id        text NOT NULL REFERENCES channels (id),
    state             text NOT NULL DEFAULT 'planned' REFERENCES job_states (state),
    origin            text NOT NULL
        CHECK (origin IN ('owner', 'planner', 'trigger', 'experiment', 'derived')),
    parent_job_id     uuid REFERENCES video_jobs (id),
    content_hash      text CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    approval_deadline timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, channel_id),
    CHECK (origin <> 'derived' OR parent_job_id IS NOT NULL)
);
CREATE INDEX video_jobs_channel_state_idx ON video_jobs (channel_id, state);

CREATE TABLE approvals (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id       uuid NOT NULL,
    channel_id   text NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    token_id     text NOT NULL UNIQUE,
    approver     text NOT NULL CHECK (length(approver) > 0),
    issued_at    timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz,
    FOREIGN KEY (job_id, channel_id) REFERENCES video_jobs (id, channel_id),
    CHECK (expires_at > issued_at)
);
CREATE INDEX approvals_job_idx ON approvals (job_id);

CREATE TABLE job_events (
    id         bigserial PRIMARY KEY,
    job_id     uuid NOT NULL REFERENCES video_jobs (id),
    from_state text REFERENCES job_states (state),
    to_state   text NOT NULL REFERENCES job_states (state),
    actor      text NOT NULL DEFAULT current_user,
    at         timestamptz NOT NULL DEFAULT now(),
    payload    jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX job_events_job_idx ON job_events (job_id, id);

CREATE TABLE audit_events (
    id        bigserial PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor     text NOT NULL,
    action    text NOT NULL,
    subject   text NOT NULL,
    payload   jsonb NOT NULL DEFAULT '{}'::jsonb,
    prev_hash bytea,
    hash      bytea NOT NULL
);

-- ---------------------------------------------------------------- append-only guards

CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (% rejected)', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER job_events_append_only
    BEFORE UPDATE OR DELETE ON job_events
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER job_events_no_truncate
    BEFORE TRUNCATE ON job_events
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER audit_events_append_only
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER audit_events_no_truncate
    BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- audit hash chain

CREATE FUNCTION audit_event_digest(prev bytea, e audit_events) RETURNS bytea
LANGUAGE sql IMMUTABLE AS $$
    SELECT sha256(
        coalesce(prev, '\x'::bytea)
        || convert_to(
            concat_ws(E'\x1f', e.id::text, to_char(e.at AT TIME ZONE 'UTC',
                'YYYY-MM-DD"T"HH24:MI:SS.US'), e.actor, e.action, e.subject, e.payload::text),
            'UTF8'));
$$;

CREATE FUNCTION audit_events_chain() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- Serialize writers so the chain is linear.
    PERFORM pg_advisory_xact_lock(hashtext('audit_events_chain'));
    SELECT hash INTO NEW.prev_hash FROM audit_events ORDER BY id DESC LIMIT 1;
    NEW.at := clock_timestamp();
    NEW.hash := audit_event_digest(NEW.prev_hash, NEW);
    RETURN NEW;
END;
$$;

CREATE TRIGGER audit_events_chain
    BEFORE INSERT ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_chain();

-- Returns the id of the first row whose hash or link is broken, or NULL if the chain is intact.
CREATE FUNCTION audit_chain_first_break() RETURNS bigint LANGUAGE plpgsql STABLE AS $$
DECLARE
    r    audit_events;
    prev bytea := NULL;
BEGIN
    FOR r IN SELECT * FROM audit_events ORDER BY id LOOP
        IF r.prev_hash IS DISTINCT FROM prev OR r.hash <> audit_event_digest(prev, r) THEN
            RETURN r.id;
        END IF;
        prev := r.hash;
    END LOOP;
    RETURN NULL;
END;
$$;

-- ---------------------------------------------------------------- job state machine

CREATE FUNCTION video_jobs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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

    -- Once content is submitted for approval it is frozen; a change needs a new job.
    IF NEW.content_hash IS DISTINCT FROM OLD.content_hash
       AND OLD.state IN ('awaiting_approval', 'approved', 'publishing', 'published') THEN
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

CREATE TRIGGER video_jobs_guard
    BEFORE INSERT OR UPDATE ON video_jobs
    FOR EACH ROW EXECUTE FUNCTION video_jobs_guard();

CREATE FUNCTION video_jobs_record_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO job_events (job_id, from_state, to_state) VALUES (NEW.id, NULL, NEW.state);
    ELSIF NEW.state IS DISTINCT FROM OLD.state THEN
        INSERT INTO job_events (job_id, from_state, to_state) VALUES (NEW.id, OLD.state, NEW.state);
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER video_jobs_record_event
    AFTER INSERT OR UPDATE ON video_jobs
    FOR EACH ROW EXECUTE FUNCTION video_jobs_record_event();

CREATE TRIGGER video_jobs_no_delete
    BEFORE DELETE ON video_jobs
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- approvals

CREATE FUNCTION approvals_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    j video_jobs;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT * INTO j FROM video_jobs WHERE id = NEW.job_id FOR UPDATE;
        IF j.state <> 'awaiting_approval' THEN
            RAISE EXCEPTION 'approvals can only be recorded for jobs awaiting approval (job is %)',
                j.state USING ERRCODE = 'check_violation';
        END IF;
        IF j.content_hash IS DISTINCT FROM NEW.content_hash THEN
            RAISE EXCEPTION 'approval content_hash does not match job content'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    -- UPDATE: the only permitted change is a one-way revocation.
    IF OLD.revoked_at IS NOT NULL
       OR NEW.revoked_at IS NULL
       OR (to_jsonb(NEW) - 'revoked_at') <> (to_jsonb(OLD) - 'revoked_at') THEN
        RAISE EXCEPTION 'approvals are immutable except for a single revocation'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER approvals_guard
    BEFORE INSERT OR UPDATE ON approvals
    FOR EACH ROW EXECUTE FUNCTION approvals_guard();
CREATE TRIGGER approvals_no_delete
    BEFORE DELETE ON approvals
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER approvals_no_truncate
    BEFORE TRUNCATE ON approvals
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

COMMIT;
