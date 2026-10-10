-- 009: safety hardening found in review of 008.
--  * kill switches fail CLOSED: a missing global_controls row counts as paused, and the table
--    cannot be truncated
--  * script fingerprints must have a sane embedding: same dimension per model, non-zero norm
BEGIN;

CREATE OR REPLACE FUNCTION channel_not_paused(p_channel text) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT NOT coalesce((SELECT publishing_paused FROM global_controls), true)
       AND EXISTS (
        SELECT 1 FROM channels c LEFT JOIN clusters k ON k.id = c.cluster_id
        WHERE c.id = p_channel AND c.status = 'active' AND coalesce(k.status, 'active') = 'active');
$$;

CREATE OR REPLACE FUNCTION approvals_kill_switch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF channel_not_paused(NEW.channel_id) IS NOT TRUE THEN
        RAISE EXCEPTION 'approvals are refused while a kill switch is engaged for channel %',
            NEW.channel_id USING ERRCODE = 'YTC01';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER global_controls_no_truncate BEFORE TRUNCATE ON global_controls
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

CREATE FUNCTION content_fingerprints_embedding_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    dims integer;
BEGIN
    IF NEW.embedding IS NULL THEN
        RETURN NEW;
    END IF;
    IF (NEW.embedding <#> NEW.embedding) = 0 THEN
        RAISE EXCEPTION 'embedding must not be the zero vector' USING ERRCODE = 'check_violation';
    END IF;
    SELECT vector_dims(embedding) INTO dims FROM content_fingerprints
        WHERE embedding_model = NEW.embedding_model AND embedding IS NOT NULL LIMIT 1;
    IF dims IS NOT NULL AND dims <> vector_dims(NEW.embedding) THEN
        RAISE EXCEPTION 'embedding model % already uses % dimensions', NEW.embedding_model, dims
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER content_fingerprints_embedding_guard BEFORE INSERT ON content_fingerprints
    FOR EACH ROW EXECUTE FUNCTION content_fingerprints_embedding_guard();

COMMIT;
