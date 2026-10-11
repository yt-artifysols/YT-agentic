-- 006: per-channel knowledge base and owner inputs.
--
-- knowledge_docs: versioned, human-owned documents per channel and kind. Anyone (agents
-- included) may submit a DRAFT; only an active hub user with the approver role can approve or
-- reject it. Approved and rejected versions are immutable; nothing is ever deleted.
-- job_knowledge: the exact approved versions each job used (reproducibility).
-- owner_inputs / job_owner_inputs: ideas, footage, notes and goals from the operator, and which
-- job consumed which input (provenance).
-- Composite (id, channel_id) foreign keys make every cross-channel link impossible.
BEGIN;

ALTER TABLE channels ADD CONSTRAINT channels_id_namespace_key UNIQUE (id, memory_namespace);

-- ---------------------------------------------------------------- knowledge

CREATE TABLE knowledge_docs (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id     text NOT NULL REFERENCES channels (id),
    kind           text NOT NULL CHECK (kind IN ('brand_voice', 'offers', 'playbook',
                       'hook_library', 'banned_claims', 'style_guide', 'policy')),
    version        integer NOT NULL CHECK (version > 0),
    -- The approved version this draft was written against (NULL for the first version).
    base_version   integer CHECK (base_version > 0),
    content        text NOT NULL CHECK (length(content) BETWEEN 1 AND 100000),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    status         text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'approved', 'rejected')),
    proposed_by    text NOT NULL CHECK (length(proposed_by) > 0),
    proposed_at    timestamptz NOT NULL DEFAULT now(),
    approved_by    text,
    approved_at    timestamptz,
    rejected_by    text,
    rejected_at    timestamptz,
    reject_reason  text,
    UNIQUE (channel_id, kind, version),
    UNIQUE (id, channel_id),
    CHECK ((status = 'approved') = (approved_by IS NOT NULL AND approved_at IS NOT NULL)),
    CHECK ((status = 'rejected') = (rejected_by IS NOT NULL AND rejected_at IS NOT NULL))
);
CREATE INDEX knowledge_docs_current_idx ON knowledge_docs (channel_id, kind, status, version);

CREATE FUNCTION knowledge_is_human_approver(p_username text) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM hub_users
                   WHERE username = p_username AND role = 'approver' AND disabled_at IS NULL);
$$;

CREATE FUNCTION knowledge_latest_version(p_channel text, p_kind text) RETURNS integer
LANGUAGE sql STABLE AS $$
    SELECT max(version) FROM knowledge_docs
    WHERE channel_id = p_channel AND kind = p_kind AND status = 'approved';
$$;

CREATE FUNCTION knowledge_docs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.approved_by IS NOT NULL OR NEW.approved_at IS NOT NULL
           OR NEW.rejected_by IS NOT NULL OR NEW.rejected_at IS NOT NULL THEN
            RAISE EXCEPTION 'knowledge versions are submitted as drafts'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NEW.base_version IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM knowledge_docs WHERE channel_id = NEW.channel_id AND kind = NEW.kind
              AND version = NEW.base_version AND status = 'approved') THEN
            RAISE EXCEPTION 'knowledge base_version % is not an approved version', NEW.base_version
                USING ERRCODE = 'check_violation';
        END IF;
        -- Versions are assigned here, serialized per channel and kind.
        PERFORM pg_advisory_xact_lock(hashtext('knowledge:' || NEW.channel_id || ':' || NEW.kind));
        SELECT coalesce(max(version), 0) + 1 INTO NEW.version FROM knowledge_docs
            WHERE channel_id = NEW.channel_id AND kind = NEW.kind;
        NEW.content_sha256 := 'sha256:' || encode(sha256(convert_to(NEW.content, 'UTF8')), 'hex');
        NEW.proposed_at := now();
        RETURN NEW;
    END IF;

    IF OLD.status <> 'draft' THEN
        RAISE EXCEPTION 'knowledge version % is % and immutable', OLD.version, OLD.status
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF (to_jsonb(NEW) - ARRAY['status', 'approved_by', 'approved_at', 'rejected_by',
                              'rejected_at', 'reject_reason'])
       <> (to_jsonb(OLD) - ARRAY['status', 'approved_by', 'approved_at', 'rejected_by',
                                 'rejected_at', 'reject_reason']) THEN
        RAISE EXCEPTION 'knowledge drafts are immutable; submit a new draft'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF NEW.status IN ('approved', 'rejected')
       AND app_actor() IS DISTINCT FROM 'hub:' || coalesce(NEW.approved_by, NEW.rejected_by) THEN
        RAISE EXCEPTION 'knowledge decisions are made through the approval hub by the approver'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status = 'approved' THEN
        -- Serialize with other approvals (and new drafts) of this channel and kind, so two
        -- drafts on the same base can never both be approved.
        PERFORM pg_advisory_xact_lock(hashtext('knowledge:' || NEW.channel_id || ':' || NEW.kind));
        IF NOT knowledge_is_human_approver(NEW.approved_by) THEN
            RAISE EXCEPTION 'knowledge approval requires an active human approver'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NEW.base_version IS DISTINCT FROM knowledge_latest_version(NEW.channel_id, NEW.kind)
        THEN
            RAISE EXCEPTION 'knowledge draft is stale: based on version %, current is %',
                NEW.base_version, knowledge_latest_version(NEW.channel_id, NEW.kind)
                USING ERRCODE = 'YTK01';
        END IF;
        NEW.approved_at := now();
        NEW.rejected_by := NULL;
        NEW.rejected_at := NULL;
    ELSIF NEW.status = 'rejected' THEN
        IF NOT knowledge_is_human_approver(NEW.rejected_by) THEN
            RAISE EXCEPTION 'knowledge rejection requires an active human approver'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF length(coalesce(NEW.reject_reason, '')) < 3 THEN
            RAISE EXCEPTION 'knowledge rejection needs a reason' USING ERRCODE = 'check_violation';
        END IF;
        NEW.rejected_at := now();
        NEW.approved_by := NULL;
        NEW.approved_at := NULL;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION knowledge_docs_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM audit('knowledge.proposed', 'channel:' || NEW.channel_id,
                      jsonb_build_object('doc', NEW.id, 'kind', NEW.kind, 'version', NEW.version,
                                         'by', NEW.proposed_by, 'sha256', NEW.content_sha256));
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        PERFORM audit('knowledge.' || NEW.status, 'channel:' || NEW.channel_id,
                      jsonb_build_object('doc', NEW.id, 'kind', NEW.kind, 'version', NEW.version,
                                         'by', coalesce(NEW.approved_by, NEW.rejected_by),
                                         'sha256', NEW.content_sha256));
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER knowledge_docs_guard BEFORE INSERT OR UPDATE ON knowledge_docs
    FOR EACH ROW EXECUTE FUNCTION knowledge_docs_guard();
CREATE TRIGGER knowledge_docs_audit AFTER INSERT OR UPDATE ON knowledge_docs
    FOR EACH ROW EXECUTE FUNCTION knowledge_docs_audit();
CREATE TRIGGER knowledge_docs_no_delete BEFORE DELETE ON knowledge_docs
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER knowledge_docs_no_truncate BEFORE TRUNCATE ON knowledge_docs
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- Knowledge versions a job used. Pinned when research starts; the latest pin per kind wins.
CREATE TABLE job_knowledge (
    job_id     uuid NOT NULL,
    channel_id text NOT NULL,
    doc_id     uuid NOT NULL,
    kind       text NOT NULL,
    version    integer NOT NULL,
    pinned_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, doc_id),
    FOREIGN KEY (job_id, channel_id) REFERENCES video_jobs (id, channel_id),
    FOREIGN KEY (doc_id, channel_id) REFERENCES knowledge_docs (id, channel_id)
);

CREATE FUNCTION job_knowledge_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    d knowledge_docs;
BEGIN
    SELECT * INTO d FROM knowledge_docs WHERE id = NEW.doc_id;
    IF d.status IS DISTINCT FROM 'approved' THEN
        RAISE EXCEPTION 'jobs may only use approved knowledge (doc % is %)', NEW.doc_id, d.status
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    NEW.kind := d.kind;
    NEW.version := d.version;
    RETURN NEW;
END;
$$;

CREATE TRIGGER job_knowledge_guard BEFORE INSERT ON job_knowledge
    FOR EACH ROW EXECUTE FUNCTION job_knowledge_guard();
CREATE TRIGGER job_knowledge_append_only BEFORE UPDATE OR DELETE ON job_knowledge
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER job_knowledge_no_truncate BEFORE TRUNCATE ON job_knowledge
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- owner inputs

CREATE TABLE owner_inputs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id   text NOT NULL REFERENCES channels (id),
    kind         text NOT NULL CHECK (kind IN ('idea', 'footage', 'note', 'goal')),
    uri          text CHECK (uri ~ '^(file|gs|s3|https)://[^\s]+$' AND length(uri) <= 2000),
    body         text CHECK (length(body) BETWEEN 1 AND 20000),
    sha256       text NOT NULL CHECK (sha256 ~ '^sha256:[0-9a-f]{64}$'),
    status       text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'used', 'archived')),
    submitted_by text NOT NULL CHECK (length(submitted_by) > 0),
    used_by_job  uuid,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, channel_id),
    FOREIGN KEY (used_by_job, channel_id) REFERENCES video_jobs (id, channel_id),
    CHECK (uri IS NOT NULL OR body IS NOT NULL),
    CHECK (kind <> 'footage' OR uri IS NOT NULL),
    -- Goals are standing context for the planner; they are never consumed by one job.
    CHECK (kind <> 'goal' OR status <> 'used'),
    CHECK ((status = 'used') = (used_by_job IS NOT NULL) OR status = 'archived')
);
CREATE INDEX owner_inputs_channel_status_idx ON owner_inputs (channel_id, status, created_at);
-- The same content is only queued once per channel.
CREATE UNIQUE INDEX owner_inputs_dedup ON owner_inputs (channel_id, kind, sha256)
    WHERE status <> 'archived';

CREATE FUNCTION owner_inputs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'new' OR NEW.used_by_job IS NOT NULL THEN
            RAISE EXCEPTION 'owner inputs start as new' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.body IS NOT NULL AND NEW.uri IS NULL AND NEW.sha256 IS DISTINCT FROM
           'sha256:' || encode(sha256(convert_to(NEW.body, 'UTF8')), 'hex') THEN
            RAISE EXCEPTION 'owner input sha256 does not match its body'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF (to_jsonb(NEW) - ARRAY['status', 'used_by_job', 'updated_at'])
       <> (to_jsonb(OLD) - ARRAY['status', 'used_by_job', 'updated_at']) THEN
        RAISE EXCEPTION 'owner input content is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.used_by_job IS DISTINCT FROM OLD.used_by_job
       AND (OLD.used_by_job IS NOT NULL
            OR current_setting('ytcore.input_link', true) IS DISTINCT FROM 'on') THEN
        RAISE EXCEPTION 'owner inputs are marked used only by linking them to a job'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'new' AND NEW.status IN ('used', 'archived')) OR
        (OLD.status = 'used' AND NEW.status = 'archived')) THEN
        RAISE EXCEPTION 'illegal owner input transition % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE FUNCTION owner_inputs_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM audit('input.submitted', 'channel:' || NEW.channel_id,
                      jsonb_build_object('input', NEW.id, 'kind', NEW.kind, 'sha256', NEW.sha256,
                                         'by', NEW.submitted_by));
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        PERFORM audit('input.' || NEW.status, 'channel:' || NEW.channel_id,
                      jsonb_build_object('input', NEW.id, 'job', NEW.used_by_job));
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER owner_inputs_guard BEFORE INSERT OR UPDATE ON owner_inputs
    FOR EACH ROW EXECUTE FUNCTION owner_inputs_guard();
CREATE TRIGGER owner_inputs_audit AFTER INSERT OR UPDATE ON owner_inputs
    FOR EACH ROW EXECUTE FUNCTION owner_inputs_audit();
CREATE TRIGGER owner_inputs_no_delete BEFORE DELETE ON owner_inputs
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER owner_inputs_no_truncate BEFORE TRUNCATE ON owner_inputs
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- Which owner inputs a job used. Linking a consumable input (idea, footage, note) marks it used
-- by that job; goals are linked as context and stay available.
CREATE TABLE job_owner_inputs (
    job_id     uuid NOT NULL,
    channel_id text NOT NULL,
    input_id   uuid NOT NULL,
    linked_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, input_id),
    FOREIGN KEY (job_id, channel_id) REFERENCES video_jobs (id, channel_id),
    FOREIGN KEY (input_id, channel_id) REFERENCES owner_inputs (id, channel_id)
);

CREATE FUNCTION job_owner_inputs_link() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    i owner_inputs;
BEGIN
    SELECT * INTO i FROM owner_inputs WHERE id = NEW.input_id FOR UPDATE;
    IF i.status = 'archived' THEN
        RAISE EXCEPTION 'owner input % is archived', NEW.input_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF i.kind <> 'goal' THEN
        IF i.status = 'used' AND i.used_by_job IS DISTINCT FROM NEW.job_id THEN
            RAISE EXCEPTION 'owner input % already used by job %', NEW.input_id, i.used_by_job
                USING ERRCODE = 'check_violation';
        END IF;
        IF i.status = 'new' THEN
            PERFORM set_config('ytcore.input_link', 'on', true);
            UPDATE owner_inputs SET status = 'used', used_by_job = NEW.job_id
            WHERE id = NEW.input_id;
            PERFORM set_config('ytcore.input_link', 'off', true);
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER job_owner_inputs_link BEFORE INSERT ON job_owner_inputs
    FOR EACH ROW EXECUTE FUNCTION job_owner_inputs_link();
CREATE TRIGGER job_owner_inputs_append_only BEFORE UPDATE OR DELETE ON job_owner_inputs
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER job_owner_inputs_no_truncate BEFORE TRUNCATE ON job_owner_inputs
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

COMMIT;
