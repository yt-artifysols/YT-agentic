-- 011: scale to 20 channels.
--  * cluster settings (niche, concurrency, budget) and the ROLLOUT gate: a cluster that requires
--    another cannot go live until that one has run live for gate_days and a human admin signed
--    it off; channels of a not-live cluster cannot be gated (publishing)
--  * fair-share limits and leases for the shared agent pools (priority = nearest deadline)
--  * channel publish window and per-pool config
BEGIN;

-- ---------------------------------------------------------------- clusters and rollout

ALTER TABLE clusters
    ADD COLUMN niche               text NOT NULL DEFAULT '',
    ADD COLUMN requires_cluster    text REFERENCES clusters (id),
    -- Existing clusters (the pilot) stay live. New ones start dark unless config says otherwise.
    ADD COLUMN live_enabled        boolean NOT NULL DEFAULT true,
    ADD COLUMN live_since          timestamptz,
    ADD COLUMN gate_days           integer NOT NULL DEFAULT 14 CHECK (gate_days BETWEEN 1 AND 90),
    ADD COLUMN max_concurrent_jobs integer NOT NULL DEFAULT 4 CHECK (max_concurrent_jobs >= 1),
    ADD COLUMN daily_budget_cents  integer CHECK (daily_budget_cents >= 0),
    ADD CONSTRAINT clusters_not_self_requiring CHECK (requires_cluster IS DISTINCT FROM id);

UPDATE clusters SET live_since = created_at WHERE live_enabled;

CREATE TABLE rollout_signoffs (
    id         bigserial PRIMARY KEY,
    cluster_id text NOT NULL REFERENCES clusters (id),
    reviewer   text NOT NULL,
    note       text NOT NULL CHECK (length(btrim(note)) >= 20),
    evidence   jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rollout_signoffs_cluster_idx ON rollout_signoffs (cluster_id, created_at);

-- A sign-off is a human decision: the reviewer must be an active admin of the approval hub.
CREATE FUNCTION rollout_signoffs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM hub_users WHERE username = NEW.reviewer AND role = 'admin'
                   AND disabled_at IS NULL) THEN
        RAISE EXCEPTION 'rollout sign-off requires an active hub admin (got %)', NEW.reviewer
            USING ERRCODE = 'YTR01';
    END IF;
    PERFORM audit('rollout.signoff', 'cluster:' || NEW.cluster_id,
                  jsonb_build_object('reviewer', NEW.reviewer));
    RETURN NEW;
END;
$$;
CREATE TRIGGER rollout_signoffs_guard BEFORE INSERT ON rollout_signoffs
    FOR EACH ROW EXECUTE FUNCTION rollout_signoffs_guard();
CREATE TRIGGER rollout_signoffs_append_only BEFORE UPDATE OR DELETE ON rollout_signoffs
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER rollout_signoffs_no_truncate BEFORE TRUNCATE ON rollout_signoffs
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- Enabling a cluster for live publishing is gated on its prerequisite.
CREATE FUNCTION clusters_rollout_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    prereq clusters;
BEGIN
    IF NEW.live_enabled AND (TG_OP = 'INSERT' OR NOT OLD.live_enabled) THEN
        IF NEW.requires_cluster IS NOT NULL THEN
            SELECT * INTO prereq FROM clusters WHERE id = NEW.requires_cluster;
            IF NOT prereq.live_enabled OR prereq.live_since IS NULL
               OR prereq.live_since > now() - make_interval(days => NEW.gate_days) THEN
                RAISE EXCEPTION 'cluster % must run live for % days before % is enabled',
                    NEW.requires_cluster, NEW.gate_days, NEW.id USING ERRCODE = 'YTR01';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM rollout_signoffs s
                WHERE s.cluster_id = NEW.requires_cluster
                  AND s.created_at >= prereq.live_since + make_interval(days => NEW.gate_days)) THEN
                RAISE EXCEPTION 'cluster % needs a human sign-off after its % day gate',
                    NEW.requires_cluster, NEW.gate_days USING ERRCODE = 'YTR01';
            END IF;
        END IF;
        NEW.live_since := now();
        PERFORM audit('rollout.enabled', 'cluster:' || NEW.id,
                      jsonb_build_object('requires', NEW.requires_cluster));
    ELSIF TG_OP = 'UPDATE' AND NOT NEW.live_enabled AND OLD.live_enabled THEN
        NEW.live_since := NULL;
        PERFORM audit('rollout.disabled', 'cluster:' || NEW.id, '{}'::jsonb);
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER clusters_rollout_guard BEFORE INSERT OR UPDATE OF live_enabled ON clusters
    FOR EACH ROW EXECUTE FUNCTION clusters_rollout_guard();

-- Channels of a cluster that is not live stay in shadow.
CREATE FUNCTION channels_rollout_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.autonomy_tier = 'gated' AND NEW.cluster_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM clusters k WHERE k.id = NEW.cluster_id AND NOT k.live_enabled) THEN
        RAISE EXCEPTION 'cluster % is not enabled for live publishing', NEW.cluster_id
            USING ERRCODE = 'YTR01';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER channels_rollout_guard BEFORE INSERT OR UPDATE OF autonomy_tier, cluster_id
    ON channels FOR EACH ROW EXECUTE FUNCTION channels_rollout_guard();

-- Belt and braces: publishing also requires a live cluster.
CREATE OR REPLACE FUNCTION channel_publishable(p_channel text) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT NOT coalesce((SELECT publishing_paused FROM global_controls), true)
       AND EXISTS (
        SELECT 1 FROM channels c JOIN clusters k ON k.id = c.cluster_id
        WHERE c.id = p_channel
          AND c.status = 'active'
          AND k.status = 'active'
          AND k.live_enabled
          AND c.autonomy_tier = 'gated'
          AND c.google_project_id IS NOT NULL
          AND c.youtube_credentials_ref IS NOT NULL);
$$;

-- ---------------------------------------------------------------- publish window

ALTER TABLE channels
    -- Slots are drawn from [publish_hour_local, publish_hour_local + window) in the audience time
    -- zone; 0 keeps the exact hour (pilot behaviour).
    ADD COLUMN publish_window_minutes integer NOT NULL DEFAULT 0
        CHECK (publish_window_minutes BETWEEN 0 AND 720);

-- ---------------------------------------------------------------- fair-share pools

CREATE TABLE pool_limits (
    scope          text NOT NULL CHECK (scope IN ('global', 'cluster', 'channel')),
    scope_id       text NOT NULL DEFAULT '',
    pool           text NOT NULL CHECK (pool IN ('research', 'script_seo', 'media', 'gpu_render', 'qa')),
    max_concurrent integer NOT NULL CHECK (max_concurrent >= 1),
    PRIMARY KEY (scope, scope_id, pool),
    CHECK ((scope = 'global') = (scope_id = ''))
);

-- Jobs waiting for a pool slot, ordered by how close they are to their publish deadline.
CREATE TABLE pool_waiters (
    pool        text NOT NULL,
    job_id      uuid NOT NULL,
    channel_id  text NOT NULL REFERENCES channels (id),
    cluster_id  text,
    priority_at timestamptz NOT NULL,
    since       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (pool, job_id)
);

CREATE TABLE pool_leases (
    id          bigserial PRIMARY KEY,
    pool        text NOT NULL,
    job_id      uuid NOT NULL,
    channel_id  text NOT NULL REFERENCES channels (id),
    cluster_id  text,
    acquired_at timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    released_at timestamptz
);
CREATE INDEX pool_leases_active_idx ON pool_leases (pool) WHERE released_at IS NULL;
CREATE UNIQUE INDEX pool_leases_one_per_job_pool
    ON pool_leases (pool, job_id) WHERE released_at IS NULL;

COMMIT;
