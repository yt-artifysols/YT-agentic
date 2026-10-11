-- Fix a race in the audit hash chain.
--
-- bigserial assigns audit_events.id before BEFORE INSERT triggers run, i.e. before the chain
-- lock is taken. Two concurrent writers could therefore get ids in one order and link the
-- chain in the other, and audit_chain_first_break() (which walks by id) reported a break.
-- Assign the id inside the lock so id order always equals chain order.

CREATE OR REPLACE FUNCTION audit_events_chain() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- Serialize writers so the chain is linear.
    PERFORM pg_advisory_xact_lock(hashtext('audit_events_chain'));
    NEW.id := nextval(pg_get_serial_sequence('audit_events', 'id'));
    SELECT hash INTO NEW.prev_hash FROM audit_events ORDER BY id DESC LIMIT 1;
    NEW.at := clock_timestamp();
    NEW.hash := audit_event_digest(NEW.prev_hash, NEW);
    RETURN NEW;
END;
$$;
