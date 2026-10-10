-- 010: pilot readiness.
--  * per-channel audience time zone and publish window; per-cluster slot spacing
--  * daily_plans: one planned job per channel per day, idempotent, with replan bookkeeping
--  * video_metrics / daily_scorecards: analytics ingestion and per-channel scorecards
BEGIN;

-- ---------------------------------------------------------------- schedule settings

ALTER TABLE channels
    ADD COLUMN audience_timezone  text NOT NULL DEFAULT 'UTC',
    ADD COLUMN publish_hour_local smallint NOT NULL DEFAULT 17
        CHECK (publish_hour_local BETWEEN 0 AND 23),
    -- The approval deadline is this many minutes before the slot, so a late approval can never
    -- publish at an odd time: a missed deadline cancels and re-plans.
    ADD COLUMN approval_lead_minutes integer NOT NULL DEFAULT 60
        CHECK (approval_lead_minutes BETWEEN 5 AND 4320);

ALTER TABLE clusters
    ADD COLUMN slot_spacing_minutes integer NOT NULL DEFAULT 30
        CHECK (slot_spacing_minutes BETWEEN 0 AND 360);

CREATE FUNCTION channels_timezone_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = NEW.audience_timezone) THEN
        RAISE EXCEPTION 'unknown time zone %', NEW.audience_timezone
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER channels_timezone_guard BEFORE INSERT OR UPDATE OF audience_timezone ON channels
    FOR EACH ROW EXECUTE FUNCTION channels_timezone_guard();

-- ---------------------------------------------------------------- daily plans

CREATE TABLE daily_plans (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id          text NOT NULL REFERENCES channels (id),
    -- The channel-local day this plan is for (the day the video is meant to publish).
    plan_date           date NOT NULL,
    attempt             integer NOT NULL DEFAULT 1 CHECK (attempt BETWEEN 1 AND 10),
    job_id              uuid NOT NULL UNIQUE,
    slot_at             timestamptz NOT NULL,
    slot_date           date NOT NULL,
    approval_deadline   timestamptz NOT NULL,
    budget_cents        integer NOT NULL CHECK (budget_cents >= 0),
    status              text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'replan_requested', 'replanned', 'closed', 'exhausted')),
    replan_reason       text,
    workflow_started_at timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (channel_id, plan_date, attempt),
    FOREIGN KEY (job_id, channel_id) REFERENCES video_jobs (id, channel_id),
    CHECK (approval_deadline <= slot_at)
);
-- At most one plan per channel per publish day, whether its job is still in flight or already
-- finished. Only a plan that was REPLACED (or exhausted) frees its day.
CREATE UNIQUE INDEX daily_plans_one_plan_per_slot_day
    ON daily_plans (channel_id, slot_date)
    WHERE status IN ('active', 'replan_requested', 'closed');
CREATE INDEX daily_plans_status_idx ON daily_plans (status, slot_at);

CREATE FUNCTION daily_plans_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.channel_id <> OLD.channel_id OR NEW.plan_date <> OLD.plan_date
           OR NEW.attempt <> OLD.attempt OR NEW.job_id <> OLD.job_id
           OR NEW.slot_at <> OLD.slot_at OR NEW.budget_cents <> OLD.budget_cents THEN
            RAISE EXCEPTION 'a plan''s channel, day, job, slot and budget are immutable'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        NEW.updated_at := now();
        IF NEW.status IS DISTINCT FROM OLD.status THEN
            PERFORM audit('plan.' || NEW.status, 'job:' || NEW.job_id,
                          jsonb_build_object('channel', NEW.channel_id, 'plan_date', NEW.plan_date,
                                             'reason', NEW.replan_reason));
        END IF;
        RETURN NEW;
    END IF;
    PERFORM audit('plan.created', 'job:' || NEW.job_id,
                  jsonb_build_object('channel', NEW.channel_id, 'plan_date', NEW.plan_date,
                                     'attempt', NEW.attempt, 'slot_at', NEW.slot_at));
    RETURN NEW;
END;
$$;
CREATE TRIGGER daily_plans_guard BEFORE INSERT OR UPDATE ON daily_plans
    FOR EACH ROW EXECUTE FUNCTION daily_plans_guard();
CREATE TRIGGER daily_plans_no_delete BEFORE DELETE ON daily_plans
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER daily_plans_no_truncate BEFORE TRUNCATE ON daily_plans
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- analytics

-- Read-only analytics OAuth token, SEPARATE from the upload token the gateway alone can read.
ALTER TABLE channels ADD COLUMN analytics_credentials_ref text
    CHECK (analytics_credentials_ref ~ '^(vault|env)://[A-Za-z0-9_./-]+$');

CREATE TABLE video_metrics (
    id                bigserial PRIMARY KEY,
    channel_id        text NOT NULL REFERENCES channels (id),
    job_id            uuid NOT NULL,
    youtube_video_id  text NOT NULL,
    metric_date       date NOT NULL,
    views             bigint NOT NULL CHECK (views >= 0),
    impressions       bigint NOT NULL DEFAULT 0 CHECK (impressions >= 0),
    ctr               numeric(6, 5) CHECK (ctr BETWEEN 0 AND 1),
    avg_view_seconds  numeric(10, 2) CHECK (avg_view_seconds >= 0),
    retention         numeric(6, 5) CHECK (retention BETWEEN 0 AND 1),
    revenue_cents     bigint CHECK (revenue_cents >= 0),
    ingested_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (youtube_video_id, metric_date),
    FOREIGN KEY (job_id, channel_id) REFERENCES video_jobs (id, channel_id)
);
CREATE INDEX video_metrics_channel_date_idx ON video_metrics (channel_id, metric_date);

-- Re-ingesting a day corrects it (metrics are restated by YouTube); every change is audited.
CREATE FUNCTION video_metrics_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM audit('metrics.ingested', 'job:' || NEW.job_id,
                  jsonb_build_object('date', NEW.metric_date, 'views', NEW.views));
    RETURN NULL;
END;
$$;
CREATE TRIGGER video_metrics_audit AFTER INSERT OR UPDATE ON video_metrics
    FOR EACH ROW EXECUTE FUNCTION video_metrics_audit();
CREATE TRIGGER video_metrics_no_delete BEFORE DELETE ON video_metrics
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- One learning per video per ISO week, so re-running ingestion never repeats memory notes.
CREATE TABLE performance_notes (
    youtube_video_id text NOT NULL,
    iso_week         text NOT NULL,
    channel_id       text NOT NULL REFERENCES channels (id),
    verdict          text NOT NULL CHECK (verdict IN ('above', 'below', 'typical')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (youtube_video_id, iso_week)
);

CREATE TABLE daily_scorecards (
    channel_id        text NOT NULL REFERENCES channels (id),
    score_date        date NOT NULL,
    jobs_planned      integer NOT NULL,
    jobs_approved     integer NOT NULL,
    jobs_rejected     integer NOT NULL,
    approval_rate     numeric(6, 5),
    policy_flags      integer NOT NULL,
    videos_published  integer NOT NULL,
    cost_cents        bigint NOT NULL,
    cost_per_video    numeric(12, 2),
    avg_ctr           numeric(6, 5),
    avg_retention     numeric(6, 5),
    views             bigint NOT NULL,
    revenue_cents     bigint,
    computed_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, score_date)
);
CREATE TRIGGER daily_scorecards_no_delete BEFORE DELETE ON daily_scorecards
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

COMMIT;
