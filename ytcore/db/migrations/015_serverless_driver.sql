-- Serverless staging (Vercel): no long-running worker, so durable state the services used to
-- keep in memory or in Temporal lives in the database.
--
--   hub_ceremonies     WebAuthn challenges and step-up tickets, shared by every hub instance
--   workflow_decisions mailbox from the hub (decision + approval token) to the job driver
--   driver_state       per-job driver lease and the artifacts needed to resume a job
--   asset_blobs        content-addressed assets for staging (tiny, fake renders only)
-- None of this changes who may approve: the hub still alone signs and records approvals; the
-- driver only ever accepts an approval the hub already recorded.

CREATE TABLE hub_ceremonies (
    key          text PRIMARY KEY,
    kind         text NOT NULL,
    challenge    bytea NOT NULL,
    expires_at   timestamptz NOT NULL,
    user_id      uuid,
    invite_token text,  -- one-time code, erased with the row (5 minute ceremony)
    subject      text
);
CREATE INDEX hub_ceremonies_expiry_idx ON hub_ceremonies (expires_at);

CREATE TABLE workflow_decisions (
    id            bigserial PRIMARY KEY,
    job_id        uuid NOT NULL REFERENCES video_jobs (id),
    action        text NOT NULL CHECK (action IN ('approve', 'edit', 'reject')),
    actor         text NOT NULL CHECK (length(actor) > 0),
    content_hash  text NOT NULL,
    token         text,
    rework_stage  text,
    reason        text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    consumed_at   timestamptz,
    CHECK (action <> 'approve' OR token IS NOT NULL OR consumed_at IS NOT NULL)
);
CREATE INDEX workflow_decisions_pending_idx ON workflow_decisions (job_id) WHERE consumed_at IS NULL;

-- The approval token is only useful for the exact content it was issued for and expires; it is
-- erased the moment the driver consumes the decision.
CREATE FUNCTION workflow_decisions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'workflow_decisions rows are never deleted';
    END IF;
    IF OLD.consumed_at IS NOT NULL THEN
        RAISE EXCEPTION 'a consumed decision cannot change';
    END IF;
    IF (NEW.id, NEW.job_id, NEW.action, NEW.actor, NEW.content_hash, NEW.created_at)
       IS DISTINCT FROM (OLD.id, OLD.job_id, OLD.action, OLD.actor, OLD.content_hash, OLD.created_at) THEN
        RAISE EXCEPTION 'only consumed_at may be set on a decision';
    END IF;
    NEW.token := NULL;
    RETURN NEW;
END $$;
CREATE TRIGGER workflow_decisions_guard BEFORE UPDATE OR DELETE ON workflow_decisions
    FOR EACH ROW EXECUTE FUNCTION workflow_decisions_guard();

CREATE TABLE driver_state (
    job_id        uuid PRIMARY KEY REFERENCES video_jobs (id),
    artifacts     jsonb NOT NULL DEFAULT '{}',
    rework_count  int NOT NULL DEFAULT 0,
    attempts      int NOT NULL DEFAULT 0,  -- failed drives in a row; the job fails at the limit
    locked_until  timestamptz NOT NULL DEFAULT '-infinity',
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE asset_blobs (
    key   text PRIMARY KEY,  -- <channel storage prefix>assets/<sha256 hex>
    data  bytea NOT NULL CHECK (octet_length(data) <= 5242880)
);

REVOKE ALL ON hub_ceremonies, workflow_decisions, driver_state, asset_blobs FROM PUBLIC;
GRANT USAGE ON SEQUENCE workflow_decisions_id_seq TO yt_hub, yt_worker;
GRANT SELECT, INSERT, DELETE ON hub_ceremonies TO yt_hub;
GRANT SELECT, INSERT ON workflow_decisions TO yt_hub;
GRANT SELECT ON workflow_decisions TO yt_worker;
GRANT UPDATE (consumed_at) ON workflow_decisions TO yt_worker;
GRANT SELECT, INSERT, UPDATE ON driver_state TO yt_worker;
GRANT SELECT ON driver_state TO yt_hub, yt_ops;
GRANT INSERT ON asset_blobs TO yt_worker;
GRANT SELECT ON asset_blobs TO yt_hub, yt_worker, yt_gateway;
