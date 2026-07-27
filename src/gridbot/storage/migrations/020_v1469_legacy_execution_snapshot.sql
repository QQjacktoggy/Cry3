-- Crash-safe append-only storage for dynamic LEGACY_CONTROL geometry.
-- Migration runners may be interrupted between statements and their marker,
-- therefore this migration deliberately uses only rerunnable CREATE objects.
CREATE TABLE IF NOT EXISTS v1469_arm_evidence_profile_payloads (
    evidence_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('SHADOW', 'PAID')),
    execution_profile_id TEXT NOT NULL CHECK(execution_profile_id = 'LEGACY_CONTROL'),
    execution_profile_schema TEXT NOT NULL,
    execution_profile_hash TEXT NOT NULL CHECK(
        length(execution_profile_hash) = 64
        AND execution_profile_hash NOT GLOB '*[^0-9a-f]*'
    ),
    canonical_payload_json TEXT NOT NULL CHECK(
        json_valid(canonical_payload_json)
        AND length(canonical_payload_json) BETWEEN 2 AND 32768
    ),
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
    FOREIGN KEY(evidence_id) REFERENCES v1469_arm_evidence(evidence_id),
    FOREIGN KEY(opportunity_id) REFERENCES v1469_market_opportunities(opportunity_id),
    FOREIGN KEY(candidate_id) REFERENCES v1469_lane_candidates(candidate_id),
    UNIQUE(opportunity_id, candidate_id, source_type, execution_profile_id)
);

CREATE TRIGGER IF NOT EXISTS trg_v1469_profile_payload_identity
BEFORE INSERT ON v1469_arm_evidence_profile_payloads
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM v1469_arm_evidence e
        WHERE e.evidence_id = NEW.evidence_id
          AND e.opportunity_id = NEW.opportunity_id
          AND e.candidate_id = NEW.candidate_id
          AND e.source_type = NEW.source_type
          AND e.execution_profile_id = NEW.execution_profile_id
          AND e.execution_profile_schema = NEW.execution_profile_schema
          AND e.execution_profile_hash = NEW.execution_profile_hash
    ) THEN RAISE(ABORT, 'v1469 profile payload identity mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS trg_v1469_profile_payload_no_update
BEFORE UPDATE ON v1469_arm_evidence_profile_payloads
BEGIN
    SELECT RAISE(ABORT, 'v1469 profile payload is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_v1469_profile_payload_no_delete
BEFORE DELETE ON v1469_arm_evidence_profile_payloads
BEGIN
    SELECT RAISE(ABORT, 'v1469 profile payload is append-only');
END;
