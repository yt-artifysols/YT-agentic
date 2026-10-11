-- Phase 6: risk-based approval tiers. A tier only decides HOW a human reviews (one by one, or a
-- one-tap fast lane for low-risk videos). It never removes the human: the approval gate, the
-- Ed25519 token and the gateway check are untouched, and nothing here is read by the gateway.
--   tier 1 = approve-all (default), tier 2 = risk-based.
-- Promotion needs a recommendation that a hub ADMIN confirmed in the same transaction.
-- Demotion is automatic (strike, rejection spike, policy flag) and needs a recorded reason.
ALTER TABLE channels
    ADD COLUMN approval_tier       smallint NOT NULL DEFAULT 1 CHECK (approval_tier IN (1, 2)),
    ADD COLUMN approval_tier_since timestamptz NOT NULL DEFAULT now();

-- Rules come from config/platform.yaml (applied by `ytctl channel apply`); one row.
CREATE TABLE tier_rules (
    singleton  boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    rules      jsonb NOT NULL CHECK (jsonb_typeof(rules) = 'object'),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE channel_strikes (
    id          bigserial PRIMARY KEY,
    channel_id  text NOT NULL REFERENCES channels (id),
    kind        text NOT NULL CHECK (kind IN ('strike', 'warning', 'takedown')),
    video_ref   text,
    note        text NOT NULL CHECK (length(btrim(note)) >= 10),
    recorded_by text NOT NULL DEFAULT app_actor(),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX channel_strikes_channel_idx ON channel_strikes (channel_id, created_at);
CREATE TRIGGER channel_strikes_append_only BEFORE UPDATE OR DELETE ON channel_strikes
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER channel_strikes_no_truncate BEFORE TRUNCATE ON channel_strikes
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

CREATE TABLE tier_recommendations (
    id          bigserial PRIMARY KEY,
    channel_id  text NOT NULL REFERENCES channels (id),
    evidence    jsonb NOT NULL DEFAULT '{}'::jsonb,
    status      text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'confirmed', 'dismissed', 'superseded')),
    note        text,
    decided_by  text,
    decided_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX tier_recommendations_one_pending
    ON tier_recommendations (channel_id) WHERE status = 'pending';

CREATE TABLE tier_changes (
    id                bigserial PRIMARY KEY,
    channel_id        text NOT NULL REFERENCES channels (id),
    from_tier         smallint NOT NULL,
    to_tier           smallint NOT NULL,
    kind              text NOT NULL CHECK (kind IN ('promotion', 'demotion')),
    reason            text NOT NULL CHECK (length(btrim(reason)) >= 5),
    actor             text NOT NULL DEFAULT app_actor(),
    evidence          jsonb NOT NULL DEFAULT '{}'::jsonb,
    recommendation_id bigint REFERENCES tier_recommendations (id),
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tier_changes_channel_idx ON tier_changes (channel_id, created_at);

-- tier_changes rows are written only by the channels trigger below.
CREATE FUNCTION tier_changes_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'tier changes are recorded by changing channels.approval_tier'
            USING ERRCODE = 'YTT01';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER tier_changes_guard BEFORE INSERT ON tier_changes
    FOR EACH ROW EXECUTE FUNCTION tier_changes_guard();
CREATE TRIGGER tier_changes_append_only BEFORE UPDATE OR DELETE ON tier_changes
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER tier_changes_no_truncate BEFORE TRUNCATE ON tier_changes
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- A recommendation is decided once, by an active hub admin (or superseded by the system).
CREATE FUNCTION tier_recommendations_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'tier recommendations are append-only' USING ERRCODE = 'YTT01';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending' THEN
            RAISE EXCEPTION 'a recommendation starts pending' USING ERRCODE = 'YTT01';
        END IF;
        PERFORM audit('tier.recommended', 'channel:' || NEW.channel_id, NEW.evidence);
        RETURN NEW;
    END IF;
    IF OLD.status <> 'pending' OR NEW.status = 'pending'
       OR (to_jsonb(NEW) - ARRAY['status', 'note', 'decided_by', 'decided_at'])
          <> (to_jsonb(OLD) - ARRAY['status', 'note', 'decided_by', 'decided_at']) THEN
        RAISE EXCEPTION 'a decided recommendation cannot change' USING ERRCODE = 'YTT01';
    END IF;
    IF NEW.status IN ('confirmed', 'dismissed') THEN
        IF NOT EXISTS (SELECT 1 FROM hub_users WHERE username = NEW.decided_by
                       AND role = 'admin' AND disabled_at IS NULL) THEN
            RAISE EXCEPTION 'only an active hub admin decides a tier recommendation (got %)',
                NEW.decided_by USING ERRCODE = 'YTT01';
        END IF;
        IF NEW.note IS NULL OR length(btrim(NEW.note)) < 10 THEN
            RAISE EXCEPTION 'a decision needs a note of at least 10 characters'
                USING ERRCODE = 'YTT01';
        END IF;
    END IF;
    NEW.decided_at := now();
    PERFORM audit('tier.recommendation_' || NEW.status, 'channel:' || NEW.channel_id,
                  jsonb_build_object('id', NEW.id, 'by', NEW.decided_by));
    RETURN NEW;
END;
$$;
CREATE TRIGGER tier_recommendations_guard BEFORE INSERT OR UPDATE OR DELETE
    ON tier_recommendations FOR EACH ROW EXECUTE FUNCTION tier_recommendations_guard();
CREATE TRIGGER tier_recommendations_no_truncate BEFORE TRUNCATE ON tier_recommendations
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- The only way a channel's approval tier changes.
CREATE FUNCTION channels_tier_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    rec      tier_recommendations;
    why      text;
    evidence jsonb;
BEGIN
    IF NEW.approval_tier = OLD.approval_tier THEN
        RETURN NEW;
    END IF;
    IF NEW.approval_tier = 2 THEN
        IF NEW.autonomy_tier <> 'gated' THEN
            RAISE EXCEPTION 'channel % is not on the gated tier', NEW.id USING ERRCODE = 'YTT01';
        END IF;
        SELECT * INTO rec FROM tier_recommendations
         WHERE channel_id = NEW.id AND status = 'confirmed' AND decided_at = now()
         ORDER BY id DESC LIMIT 1;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'promotion of % needs an admin-confirmed recommendation', NEW.id
                USING ERRCODE = 'YTT01';
        END IF;
        INSERT INTO tier_changes (channel_id, from_tier, to_tier, kind, reason, evidence,
                                  recommendation_id)
        VALUES (NEW.id, OLD.approval_tier, 2, 'promotion', rec.note, rec.evidence, rec.id);
        PERFORM audit('tier.promoted', 'channel:' || NEW.id,
                      jsonb_build_object('recommendation', rec.id, 'by', rec.decided_by));
    ELSE
        why := btrim(coalesce(current_setting('app.tier_reason', true), ''));
        IF length(why) < 5 THEN
            RAISE EXCEPTION 'a demotion of % needs a recorded reason', NEW.id
                USING ERRCODE = 'YTT01';
        END IF;
        evidence := coalesce(nullif(current_setting('app.tier_evidence', true), '')::jsonb,
                             '{}'::jsonb);
        INSERT INTO tier_changes (channel_id, from_tier, to_tier, kind, reason, evidence)
        VALUES (NEW.id, OLD.approval_tier, 1, 'demotion', why, evidence);
        UPDATE tier_recommendations SET status = 'superseded'
         WHERE channel_id = NEW.id AND status = 'pending';
        INSERT INTO alerts (channel_id, kind, message)
        VALUES (NEW.id, 'tier.demoted',
                left('channel ' || NEW.id || ' returned to approve-all: ' || why, 2000));
        PERFORM audit('tier.demoted', 'channel:' || NEW.id, jsonb_build_object('reason', why));
    END IF;
    NEW.approval_tier_since := now();
    RETURN NEW;
END;
$$;
CREATE TRIGGER channels_tier_guard BEFORE UPDATE OF approval_tier ON channels
    FOR EACH ROW EXECUTE FUNCTION channels_tier_guard();

-- A strike, warning or takedown returns the channel to approve-all at once.
CREATE FUNCTION channel_strikes_demote() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM set_config('app.tier_reason',
                       NEW.kind || ' recorded: ' || left(NEW.note, 200), true);
    PERFORM set_config('app.tier_evidence',
                       jsonb_build_object('strike_id', NEW.id, 'kind', NEW.kind)::text, true);
    UPDATE channels SET approval_tier = 1 WHERE id = NEW.channel_id AND approval_tier = 2;
    PERFORM audit('tier.strike', 'channel:' || NEW.channel_id,
                  jsonb_build_object('strike_id', NEW.id, 'kind', NEW.kind));
    RETURN NULL;
END;
$$;
CREATE TRIGGER channel_strikes_demote AFTER INSERT ON channel_strikes
    FOR EACH ROW EXECUTE FUNCTION channel_strikes_demote();

-- Whether a given content version is a spot check is drawn once and stored.
CREATE TABLE job_review_routing (
    job_id       uuid NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    channel_id   text NOT NULL,
    spot_check   boolean NOT NULL,
    percent      numeric(5, 2) NOT NULL CHECK (percent BETWEEN 0 AND 100),
    drawn_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, content_hash),
    FOREIGN KEY (job_id, channel_id) REFERENCES video_jobs (id, channel_id)
);
CREATE TRIGGER job_review_routing_append_only BEFORE UPDATE OR DELETE ON job_review_routing
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- What the human decided, next to what the risk score predicted (recalibration input).
CREATE TABLE review_outcomes (
    id           bigserial PRIMARY KEY,
    job_id       uuid NOT NULL,
    channel_id   text NOT NULL,
    content_hash text NOT NULL,
    decision     text NOT NULL CHECK (decision IN ('approved', 'rejected', 'edited')),
    lane         text NOT NULL CHECK (lane IN ('standard', 'fast', 'deep')),
    spot_check   boolean NOT NULL DEFAULT false,
    risk_score   integer CHECK (risk_score BETWEEN 0 AND 100),
    flag_count   integer NOT NULL DEFAULT 0,
    low_risk     boolean NOT NULL,
    tier         smallint NOT NULL,
    decided_by   text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (job_id, channel_id) REFERENCES video_jobs (id, channel_id)
);
CREATE INDEX review_outcomes_channel_idx ON review_outcomes (channel_id, created_at);
CREATE TRIGGER review_outcomes_append_only BEFORE UPDATE OR DELETE ON review_outcomes
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER review_outcomes_no_truncate BEFORE TRUNCATE ON review_outcomes
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();
