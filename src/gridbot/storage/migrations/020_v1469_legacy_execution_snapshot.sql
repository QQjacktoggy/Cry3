-- Durable exact LEGACY_CONTROL profile used to resume paired evidence after a
-- process restart.  NULL is intentional for adaptive-only evidence rows.
ALTER TABLE v1469_arm_evidence
ADD COLUMN execution_profile_payload_json TEXT CHECK (
    execution_profile_payload_json IS NULL OR (
        json_valid(execution_profile_payload_json)
        AND length(execution_profile_payload_json) <= 32768
    )
);

CREATE TRIGGER IF NOT EXISTS trg_v1469_arm_evidence_profile_payload_immutable
BEFORE UPDATE OF execution_profile_payload_json ON v1469_arm_evidence
WHEN NEW.execution_profile_payload_json IS NOT OLD.execution_profile_payload_json
BEGIN
    SELECT RAISE(ABORT, 'v1469 execution profile payload is immutable');
END;
