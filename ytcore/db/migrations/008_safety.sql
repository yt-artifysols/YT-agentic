-- 008: safety and compliance.
--  * approvals are refused while ANY kill switch (global, cluster, channel) is engaged
--  * risk assessments stored on the job (append-only, audited)
--  * cross-channel content fingerprints (append-only)
--  * cluster circuit breaker: per-cluster thresholds and an append-only trip log; alerts can
--    be about a cluster
BEGIN;

-- ---------------------------------------------------------------- kill switch on approvals

-- False when the global kill switch, the channel's cluster or the channel itself is paused.
-- (channel_publishable additionally requires the gated tier, credentials and a project.)
CREATE FUNCTION channel_not_paused(p_channel text) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT NOT (SELECT publishing_paused FROM global_controls)
       AND EXISTS (
        SELECT 1 FROM channels c LEFT JOIN clusters k ON k.id = c.cluster_id
        WHERE c.id = p_channel AND c.status = 'active' AND coalesce(k.status, 'active') = 'active');
$$;

CREATE FUNCTION approvals_kill_switch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT channel_not_paused(NEW.channel_id) THEN
        RAISE EXCEPTION 'approvals are refused while a kill switch is engaged for channel %',
            NEW.channel_id USING ERRCODE = 'YTC01';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER approvals_kill_switch BEFORE INSERT ON approvals
    FOR EACH ROW EXECUTE FUNCTION approvals_kill_switch();

-- ---------------------------------------------------------------- risk on the job

CREATE TABLE job_risk (
    id         bigserial PRIMARY KEY,
    job_id     uuid NOT NULL,
    channel_id text NOT NULL,
    score      numeric(5, 4) NOT NULL CHECK (score BETWEEN 0 AND 1),
    escalate   boolean NOT NULL,
    blocked    boolean NOT NULL,
    reasons    jsonb NOT NULL CHECK (jsonb_typeof(reasons) = 'array'),
    thresholds jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by text NOT NULL DEFAULT app_actor(),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (job_id, channel_id) REFERENCES video_jobs (id, channel_id)
);
CREATE INDEX job_risk_job_idx ON job_risk (job_id, id);

CREATE FUNCTION job_risk_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM audit('risk.assessed', 'job:' || NEW.job_id,
                  jsonb_build_object('score', NEW.score, 'escalate', NEW.escalate,
                                     'blocked', NEW.blocked));
    RETURN NULL;
END;
$$;
CREATE TRIGGER job_risk_audit AFTER INSERT ON job_risk
    FOR EACH ROW EXECUTE FUNCTION job_risk_audit();
CREATE TRIGGER job_risk_append_only BEFORE UPDATE OR DELETE ON job_risk
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER job_risk_no_truncate BEFORE TRUNCATE ON job_risk
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- content fingerprints

-- Fingerprints of produced content, readable across channels by the safety service only (the
-- duplicate detector). They hold hashes, shingle signatures and embeddings, never raw content.
CREATE TABLE content_fingerprints (
    id              bigserial PRIMARY KEY,
    job_id          uuid NOT NULL,
    channel_id      text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('script', 'media', 'title_template',
                        'thumbnail_text')),
    key             text NOT NULL CHECK (length(key) BETWEEN 1 AND 500),
    label           text NOT NULL DEFAULT '',
    minhash         bigint[],
    embedding       vector,
    embedding_model text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, kind, key),
    FOREIGN KEY (job_id, channel_id) REFERENCES video_jobs (id, channel_id),
    CHECK ((kind = 'script') = (minhash IS NOT NULL AND embedding IS NOT NULL
                                AND embedding_model IS NOT NULL))
);
CREATE INDEX content_fingerprints_lookup_idx ON content_fingerprints (kind, key, created_at);
CREATE INDEX content_fingerprints_channel_idx ON content_fingerprints (channel_id, kind);
CREATE TRIGGER content_fingerprints_append_only BEFORE UPDATE OR DELETE ON content_fingerprints
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER content_fingerprints_no_truncate BEFORE TRUNCATE ON content_fingerprints
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- cluster circuit breaker

CREATE TABLE cluster_breaker_config (
    cluster_id         text PRIMARY KEY REFERENCES clusters (id),
    enabled            boolean NOT NULL DEFAULT true,
    window_hours       integer NOT NULL DEFAULT 24 CHECK (window_hours BETWEEN 1 AND 720),
    min_decisions      integer NOT NULL DEFAULT 5 CHECK (min_decisions >= 1),
    max_rejection_rate numeric(4, 3) NOT NULL DEFAULT 0.5
        CHECK (max_rejection_rate BETWEEN 0 AND 1),
    max_flags_per_job  numeric(6, 2) NOT NULL DEFAULT 3 CHECK (max_flags_per_job >= 0),
    max_cost_ratio     numeric(6, 2) NOT NULL DEFAULT 3 CHECK (max_cost_ratio >= 1),
    updated_by         text NOT NULL DEFAULT app_actor(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION cluster_breaker_config_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_by := app_actor();
    NEW.updated_at := now();
    PERFORM audit('breaker.config', 'cluster:' || NEW.cluster_id, to_jsonb(NEW) - 'updated_at');
    RETURN NEW;
END;
$$;
CREATE TRIGGER cluster_breaker_config_audit BEFORE INSERT OR UPDATE ON cluster_breaker_config
    FOR EACH ROW EXECUTE FUNCTION cluster_breaker_config_audit();

CREATE TABLE breaker_trips (
    id         bigserial PRIMARY KEY,
    cluster_id text NOT NULL REFERENCES clusters (id),
    reasons    jsonb NOT NULL CHECK (jsonb_typeof(reasons) = 'array' AND jsonb_array_length(reasons) > 0),
    stats      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX breaker_trips_cluster_idx ON breaker_trips (cluster_id, id);

CREATE FUNCTION breaker_trips_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM audit('breaker.tripped', 'cluster:' || NEW.cluster_id,
                  jsonb_build_object('reasons', NEW.reasons));
    RETURN NULL;
END;
$$;
CREATE TRIGGER breaker_trips_audit AFTER INSERT ON breaker_trips
    FOR EACH ROW EXECUTE FUNCTION breaker_trips_audit();
CREATE TRIGGER breaker_trips_append_only BEFORE UPDATE OR DELETE ON breaker_trips
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER breaker_trips_no_truncate BEFORE TRUNCATE ON breaker_trips
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- Alerts may be about a cluster (circuit breaker).
ALTER TABLE alerts ADD COLUMN cluster_id text REFERENCES clusters (id);

CREATE OR REPLACE FUNCTION alerts_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM audit('alert.' || NEW.kind,
                  coalesce('job:' || NEW.job_id, 'channel:' || NEW.channel_id,
                           'cluster:' || NEW.cluster_id, 'global'),
                  jsonb_build_object('message', NEW.message));
    RETURN NULL;
END;
$$;

COMMIT;
