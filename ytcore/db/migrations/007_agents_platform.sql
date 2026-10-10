-- 007: platform services for the agent pools.
--
-- channel_memory: pgvector memory; every row carries channel_id and the channel's own memory
--                 namespace (composite FK), so a row can never sit in another channel's space.
-- rights_register: licenses for music, stock and fonts (QA reads it).
-- job_assets: versioned, content-addressed assets per job; music and stock need a valid license.
-- Per-job budget (SQLSTATE YTB02) on top of the per-channel daily budget (YTB01), provider detail
-- on cost rows, and an append-only alerts log.
BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------- memory

CREATE TABLE channel_memory (
    id              bigserial PRIMARY KEY,
    channel_id      text NOT NULL,
    namespace       text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('research_note', 'fact', 'script', 'hook',
                        'performance')),
    content         text NOT NULL CHECK (length(content) BETWEEN 1 AND 20000),
    embedding_model text NOT NULL CHECK (length(embedding_model) > 0),
    embedding       vector NOT NULL,
    source_job      uuid,
    created_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (channel_id, namespace) REFERENCES channels (id, memory_namespace),
    FOREIGN KEY (source_job, channel_id) REFERENCES video_jobs (id, channel_id)
);
CREATE INDEX channel_memory_ns_idx ON channel_memory (channel_id, namespace, embedding_model);

-- ---------------------------------------------------------------- rights and licenses

CREATE TABLE rights_register (
    license_ref text PRIMARY KEY CHECK (license_ref ~ '^lic_[a-z0-9][a-z0-9_-]{2,62}$'),
    -- NULL: licensed for every channel of this organisation.
    channel_id  text REFERENCES channels (id),
    asset_kind  text NOT NULL CHECK (asset_kind IN ('music', 'stock', 'font', 'voice')),
    provider    text NOT NULL CHECK (length(provider) > 0),
    scope       text NOT NULL DEFAULT 'youtube',
    expires_at  timestamptz,
    revoked_at  timestamptz,
    created_by  text NOT NULL DEFAULT app_actor(),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION rights_register_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW) - 'revoked_at') <> (to_jsonb(OLD) - 'revoked_at')
       OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
        RAISE EXCEPTION 'licenses are immutable; only a one-time revocation is allowed'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION rights_register_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM audit(CASE WHEN TG_OP = 'INSERT' THEN 'license.added' ELSE 'license.revoked' END,
                  'license:' || NEW.license_ref,
                  jsonb_build_object('channel', NEW.channel_id, 'kind', NEW.asset_kind));
    RETURN NULL;
END;
$$;

CREATE TRIGGER rights_register_guard BEFORE UPDATE ON rights_register
    FOR EACH ROW EXECUTE FUNCTION rights_register_guard();
CREATE TRIGGER rights_register_audit AFTER INSERT OR UPDATE ON rights_register
    FOR EACH ROW EXECUTE FUNCTION rights_register_audit();
CREATE TRIGGER rights_register_no_delete BEFORE DELETE ON rights_register
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER rights_register_no_truncate BEFORE TRUNCATE ON rights_register
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

CREATE FUNCTION license_valid(p_ref text, p_channel text) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM rights_register
                   WHERE license_ref = p_ref
                     AND (channel_id IS NULL OR channel_id = p_channel)
                     AND revoked_at IS NULL
                     AND (expires_at IS NULL OR expires_at > now()));
$$;

-- ---------------------------------------------------------------- versioned job assets

CREATE TABLE job_assets (
    id          bigserial PRIMARY KEY,
    job_id      uuid NOT NULL,
    channel_id  text NOT NULL,
    name        text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_.-]{0,62}$'),
    version     integer NOT NULL CHECK (version > 0),
    kind        text NOT NULL CHECK (kind IN ('video', 'audio', 'music', 'stock', 'image',
                    'document')),
    sha256      text NOT NULL CHECK (sha256 ~ '^sha256:[0-9a-f]{64}$'),
    media_type  text NOT NULL CHECK (length(media_type) > 0),
    license_ref text REFERENCES rights_register (license_ref),
    provider    text NOT NULL CHECK (length(provider) > 0),
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, name, version),
    FOREIGN KEY (job_id, channel_id) REFERENCES video_jobs (id, channel_id)
);

CREATE FUNCTION job_assets_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.kind IN ('music', 'stock') AND NEW.license_ref IS NULL THEN
        RAISE EXCEPTION '% assets need a license_ref', NEW.kind USING ERRCODE = 'YTL01';
    END IF;
    IF NEW.license_ref IS NOT NULL AND NOT license_valid(NEW.license_ref, NEW.channel_id) THEN
        RAISE EXCEPTION 'license % is not valid for channel %', NEW.license_ref, NEW.channel_id
            USING ERRCODE = 'YTL01';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('job_assets:' || NEW.job_id || ':' || NEW.name));
    SELECT coalesce(max(version), 0) + 1 INTO NEW.version FROM job_assets
        WHERE job_id = NEW.job_id AND name = NEW.name;
    NEW.created_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER job_assets_guard BEFORE INSERT ON job_assets
    FOR EACH ROW EXECUTE FUNCTION job_assets_guard();
CREATE TRIGGER job_assets_append_only BEFORE UPDATE OR DELETE ON job_assets
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER job_assets_no_truncate BEFORE TRUNCATE ON job_assets
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- budgets and costs

ALTER TABLE channels
    ADD COLUMN job_budget_cents integer NOT NULL DEFAULT 1000 CHECK (job_budget_cents >= 0);
ALTER TABLE job_costs
    ADD COLUMN provider text,
    ADD COLUMN detail   jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE FUNCTION job_costs_job_budget() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    budget integer;
    spent  bigint;
BEGIN
    -- job_costs_budget (fires first, by name) already holds the channel row lock.
    SELECT job_budget_cents INTO budget FROM channels WHERE id = NEW.channel_id;
    SELECT coalesce(sum(cost_cents), 0) INTO spent FROM job_costs WHERE job_id = NEW.job_id;
    IF spent + NEW.cost_cents > budget THEN
        RAISE EXCEPTION 'job budget exceeded for job % (% + % > % cents)',
            NEW.job_id, spent, NEW.cost_cents, budget USING ERRCODE = 'YTB02';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER job_costs_job_budget BEFORE INSERT ON job_costs
    FOR EACH ROW EXECUTE FUNCTION job_costs_job_budget();

-- ---------------------------------------------------------------- alerts

CREATE TABLE alerts (
    id         bigserial PRIMARY KEY,
    channel_id text REFERENCES channels (id),
    job_id     uuid REFERENCES video_jobs (id),
    kind       text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_.]{1,62}$'),
    message    text NOT NULL CHECK (length(message) BETWEEN 1 AND 2000),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX alerts_created_idx ON alerts (created_at);

CREATE FUNCTION alerts_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM audit('alert.' || NEW.kind, coalesce('job:' || NEW.job_id,
                                                 'channel:' || NEW.channel_id, 'global'),
                  jsonb_build_object('message', NEW.message));
    RETURN NULL;
END;
$$;

CREATE TRIGGER alerts_audit AFTER INSERT ON alerts
    FOR EACH ROW EXECUTE FUNCTION alerts_audit();
CREATE TRIGGER alerts_append_only BEFORE UPDATE OR DELETE ON alerts
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER alerts_no_truncate BEFORE TRUNCATE ON alerts
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

COMMIT;
