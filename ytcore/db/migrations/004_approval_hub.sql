-- 004: approval hub. Global kill switch, versioned manifests, QA reports, rejections for
-- rework, hub users with WebAuthn credentials, server-side sessions, invites and a
-- notification log. Approvals themselves stay in `approvals` (001/003).
BEGIN;

-- ---------------------------------------------------------------- global kill switch

CREATE TABLE global_controls (
    id                bool PRIMARY KEY DEFAULT true CHECK (id),
    publishing_paused boolean NOT NULL DEFAULT false,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    updated_by        text NOT NULL DEFAULT app_actor()
);
INSERT INTO global_controls (id) VALUES (true);

CREATE TRIGGER global_controls_no_delete BEFORE DELETE ON global_controls
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE FUNCTION global_controls_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    NEW.updated_by := app_actor();
    IF NEW.publishing_paused IS DISTINCT FROM OLD.publishing_paused THEN
        PERFORM audit(CASE WHEN NEW.publishing_paused THEN 'killswitch.engaged'
                           ELSE 'killswitch.released' END,
                      'global', '{}'::jsonb);
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER global_controls_audit BEFORE UPDATE ON global_controls
    FOR EACH ROW EXECUTE FUNCTION global_controls_audit();

-- Channel/cluster pause changes are kill-switch actions too: audit them.
CREATE FUNCTION status_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        PERFORM audit(CASE WHEN NEW.status = 'paused' THEN 'killswitch.engaged'
                           ELSE 'killswitch.released' END,
                      TG_ARGV[0] || ':' || NEW.id, jsonb_build_object('status', NEW.status));
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER channels_status_audit AFTER UPDATE OF status ON channels
    FOR EACH ROW EXECUTE FUNCTION status_audit('channel');
CREATE TRIGGER clusters_status_audit AFTER UPDATE OF status ON clusters
    FOR EACH ROW EXECUTE FUNCTION status_audit('cluster');

-- Publishing now also requires that the global kill switch is released.
CREATE OR REPLACE FUNCTION channel_publishable(p_channel text) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT NOT (SELECT publishing_paused FROM global_controls)
       AND EXISTS (
        SELECT 1 FROM channels c JOIN clusters k ON k.id = c.cluster_id
        WHERE c.id = p_channel
          AND c.status = 'active'
          AND k.status = 'active'
          AND c.autonomy_tier = 'gated'
          AND c.google_project_id IS NOT NULL
          AND c.youtube_credentials_ref IS NOT NULL);
$$;

-- ---------------------------------------------------------------- manifest versions

ALTER TABLE video_jobs
    ADD COLUMN manifest_version integer NOT NULL DEFAULT 0 CHECK (manifest_version >= 0);

CREATE TABLE manifest_versions (
    job_id       uuid NOT NULL REFERENCES video_jobs (id),
    version      integer NOT NULL CHECK (version > 0),
    manifest     jsonb NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    created_by   text NOT NULL DEFAULT app_actor(),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, version)
);
CREATE TRIGGER manifest_versions_append_only BEFORE UPDATE OR DELETE ON manifest_versions
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER manifest_versions_no_truncate BEFORE TRUNCATE ON manifest_versions
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- Every manifest change is versioned automatically, whoever made it (workflow or hub edit).
CREATE FUNCTION video_jobs_version_manifest() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.manifest IS NOT NULL AND NEW.manifest IS DISTINCT FROM OLD.manifest THEN
        IF NEW.content_hash IS NULL THEN
            RAISE EXCEPTION 'a manifest needs a content_hash' USING ERRCODE = 'check_violation';
        END IF;
        NEW.manifest_version := OLD.manifest_version + 1;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER video_jobs_version_manifest BEFORE UPDATE OF manifest ON video_jobs
    FOR EACH ROW EXECUTE FUNCTION video_jobs_version_manifest();

CREATE FUNCTION video_jobs_record_manifest() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.manifest_version IS DISTINCT FROM OLD.manifest_version THEN
        INSERT INTO manifest_versions (job_id, version, manifest, content_hash)
        VALUES (NEW.id, NEW.manifest_version, NEW.manifest, NEW.content_hash);
        PERFORM audit('manifest.version', 'job:' || NEW.id,
                      jsonb_build_object('version', NEW.manifest_version,
                                         'content_hash', NEW.content_hash));
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER video_jobs_record_manifest AFTER UPDATE OF manifest ON video_jobs
    FOR EACH ROW EXECUTE FUNCTION video_jobs_record_manifest();

-- ---------------------------------------------------------------- QA reports

CREATE TABLE qa_reports (
    id           bigserial PRIMARY KEY,
    job_id       uuid NOT NULL REFERENCES video_jobs (id),
    content_hash text CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    passed       boolean NOT NULL,
    risk_score   integer NOT NULL CHECK (risk_score BETWEEN 0 AND 100),
    -- Flagged claims and sources are untrusted DATA produced by agents/scrapers.
    flags        jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(flags) = 'array'),
    sources      jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(sources) = 'array'),
    created_by   text NOT NULL DEFAULT app_actor(),
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX qa_reports_job_idx ON qa_reports (job_id, id DESC);
CREATE TRIGGER qa_reports_append_only BEFORE UPDATE OR DELETE ON qa_reports
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- rejections

CREATE TABLE job_rejections (
    id           bigserial PRIMARY KEY,
    job_id       uuid NOT NULL,
    channel_id   text NOT NULL,
    content_hash text NOT NULL,
    reason       text NOT NULL CHECK (length(btrim(reason)) >= 10),
    rework_stage text NOT NULL CHECK (rework_stage IN ('research', 'script', 'render', 'discard')),
    rejected_by  text NOT NULL CHECK (length(rejected_by) > 0),
    created_at   timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (job_id, channel_id) REFERENCES video_jobs (id, channel_id)
);
CREATE INDEX job_rejections_job_idx ON job_rejections (job_id);

CREATE FUNCTION job_rejections_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    j video_jobs;
BEGIN
    SELECT * INTO j FROM video_jobs WHERE id = NEW.job_id FOR UPDATE;
    IF j.state <> 'awaiting_approval' THEN
        RAISE EXCEPTION 'only jobs awaiting approval can be rejected (job is %)', j.state
            USING ERRCODE = 'check_violation';
    END IF;
    IF j.content_hash IS DISTINCT FROM NEW.content_hash THEN
        RAISE EXCEPTION 'rejection content_hash does not match job content'
            USING ERRCODE = 'check_violation';
    END IF;
    PERFORM audit('approval.rejected', 'job:' || NEW.job_id,
                  jsonb_build_object('rework_stage', NEW.rework_stage,
                                     'content_hash', NEW.content_hash,
                                     'by', NEW.rejected_by));
    RETURN NEW;
END;
$$;
CREATE TRIGGER job_rejections_guard BEFORE INSERT ON job_rejections
    FOR EACH ROW EXECUTE FUNCTION job_rejections_guard();
CREATE TRIGGER job_rejections_append_only BEFORE UPDATE OR DELETE ON job_rejections
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- hub identity

CREATE TABLE hub_users (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username     text NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9][a-z0-9._@-]{2,63}$'),
    display_name text NOT NULL,
    role         text NOT NULL CHECK (role IN ('approver', 'operator', 'admin')),
    disabled_at  timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER hub_users_no_delete BEFORE DELETE ON hub_users
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE FUNCTION hub_users_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM audit('hub.user.created', 'user:' || NEW.username,
                      jsonb_build_object('role', NEW.role));
    ELSIF NEW.role IS DISTINCT FROM OLD.role OR NEW.disabled_at IS DISTINCT FROM OLD.disabled_at THEN
        PERFORM audit('hub.user.changed', 'user:' || NEW.username,
                      jsonb_build_object('role', NEW.role, 'disabled', NEW.disabled_at IS NOT NULL));
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER hub_users_audit AFTER INSERT OR UPDATE ON hub_users
    FOR EACH ROW EXECUTE FUNCTION hub_users_audit();

CREATE TABLE webauthn_credentials (
    credential_id bytea PRIMARY KEY,
    user_id       uuid NOT NULL REFERENCES hub_users (id),
    public_key    bytea NOT NULL,
    sign_count    bigint NOT NULL DEFAULT 0 CHECK (sign_count >= 0),
    transports    text[] NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz
);
CREATE INDEX webauthn_credentials_user_idx ON webauthn_credentials (user_id);

-- Sessions and invites store only SHA-256 hashes of their bearer secrets.
CREATE TABLE hub_sessions (
    token_hash   bytea PRIMARY KEY CHECK (length(token_hash) = 32),
    user_id      uuid NOT NULL REFERENCES hub_users (id),
    csrf_hash    bytea NOT NULL CHECK (length(csrf_hash) = 32),
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    revoked_at   timestamptz,
    CHECK (expires_at > created_at)
);
CREATE INDEX hub_sessions_user_idx ON hub_sessions (user_id);

CREATE TABLE hub_invites (
    token_hash bytea PRIMARY KEY CHECK (length(token_hash) = 32),
    user_id    uuid NOT NULL REFERENCES hub_users (id),
    expires_at timestamptz NOT NULL,
    used_at    timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- notification log

CREATE TABLE hub_notifications (
    id      bigserial PRIMARY KEY,
    kind    text NOT NULL CHECK (kind IN ('items_waiting', 'deadline_approaching', 'kill_switch')),
    dedup   text NOT NULL,
    sent_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, dedup)
);

COMMIT;
