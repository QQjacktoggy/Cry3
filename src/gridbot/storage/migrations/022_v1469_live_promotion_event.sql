-- Add the dedicated durable PROBATION -> LIVE promotion audit event.
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS trg_v1469_arm_events_no_update;
DROP TRIGGER IF EXISTS trg_v1469_arm_events_no_delete;

ALTER TABLE v1469_arm_events RENAME TO v1469_arm_events_016;

CREATE TABLE v1469_arm_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    arm_key TEXT NOT NULL,
    lease_id TEXT,
    opportunity_id TEXT,
    candidate_id TEXT,
    generation_before INTEGER CHECK(
        generation_before IS NULL OR generation_before >= 0
    ),
    generation_after INTEGER CHECK(
        generation_after IS NULL OR generation_after >= 1
    ),
    event_time_ms INTEGER NOT NULL CHECK(event_time_ms >= 0),
    event_type TEXT NOT NULL CHECK(event_type IN (
        'OBSERVED', 'EVIDENCE_STARTED', 'EVIDENCE_TERMINAL',
        'EVALUATED', 'PROBATION_GRANTED', 'LIVE_GRANTED',
        'LIVE_PROMOTED', 'LEASE_RENEWED', 'LEASE_REVOKED', 'COOLDOWN',
        'DEMOTED', 'EXPIRED', 'HALTED'
    )),
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(
        json_valid(payload_json) AND length(payload_json) <= 4096
    ),
    FOREIGN KEY(lease_id) REFERENCES v1469_arm_leases(lease_id),
    FOREIGN KEY(opportunity_id)
        REFERENCES v1469_market_opportunities(opportunity_id),
    FOREIGN KEY(candidate_id)
        REFERENCES v1469_lane_candidates(candidate_id),
    CHECK(
        generation_after IS NULL
        OR generation_before IS NULL
        OR generation_after > generation_before
    )
);

INSERT INTO v1469_arm_events (
    id, idempotency_key, arm_key, lease_id, opportunity_id, candidate_id,
    generation_before, generation_after, event_time_ms, event_type, actor,
    payload_json
)
SELECT
    id, idempotency_key, arm_key, lease_id, opportunity_id, candidate_id,
    generation_before, generation_after, event_time_ms, event_type, actor,
    payload_json
FROM v1469_arm_events_016;

DROP TABLE v1469_arm_events_016;

CREATE INDEX idx_v1469_arm_events_arm_time
ON v1469_arm_events(arm_key, event_time_ms, id);

CREATE INDEX idx_v1469_arm_events_opportunity_time
ON v1469_arm_events(opportunity_id, event_time_ms, id);

CREATE TRIGGER trg_v1469_arm_events_no_update
BEFORE UPDATE ON v1469_arm_events
BEGIN
    SELECT RAISE(ABORT, 'v1469 arm events are append-only');
END;

CREATE TRIGGER trg_v1469_arm_events_no_delete
BEFORE DELETE ON v1469_arm_events
BEGIN
    SELECT RAISE(ABORT, 'v1469 arm events are append-only');
END;

COMMIT;
PRAGMA foreign_keys = ON;
