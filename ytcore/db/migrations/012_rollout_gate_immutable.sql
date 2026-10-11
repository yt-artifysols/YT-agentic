-- The rollout gate of an existing cluster cannot be weakened by an UPDATE (for example a config
-- apply): requires_cluster cannot be cleared or changed and gate_days cannot be lowered.
-- Changing the gate means a reviewed, deliberate database change, not a YAML edit.
CREATE FUNCTION clusters_gate_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.requires_cluster IS NOT NULL AND (
           NEW.requires_cluster IS DISTINCT FROM OLD.requires_cluster
           OR NEW.gate_days < OLD.gate_days) THEN
        RAISE EXCEPTION 'the rollout gate of cluster % cannot be weakened', OLD.id
            USING ERRCODE = 'YTR01';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER clusters_gate_immutable BEFORE UPDATE OF requires_cluster, gate_days ON clusters
    FOR EACH ROW EXECUTE FUNCTION clusters_gate_immutable();
