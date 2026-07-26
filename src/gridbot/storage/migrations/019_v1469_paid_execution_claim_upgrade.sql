-- Upgrade already-installed v1.4.69 paid claims without rewriting a DB outside migrations.
-- SQLite cannot alter CHECK constraints, so rebuild both FK-related tables atomically.
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;
DROP TRIGGER IF EXISTS trg_v1469_paid_claim_opportunity_scope;
DROP TRIGGER IF EXISTS trg_v1469_paid_claim_active_lease;
DROP TRIGGER IF EXISTS trg_v1469_paid_claim_no_delete;
DROP TRIGGER IF EXISTS trg_v1469_paid_claim_terminal_once;
DROP TRIGGER IF EXISTS trg_v1469_paid_claim_transition_guard;
DROP TRIGGER IF EXISTS trg_v1469_paid_claim_event_identity;
DROP TRIGGER IF EXISTS trg_v1469_paid_claim_events_no_update;
DROP TRIGGER IF EXISTS trg_v1469_paid_claim_events_no_delete;
DROP INDEX IF EXISTS idx_v1469_paid_claim_status_time;
DROP INDEX IF EXISTS idx_v1469_paid_claim_arm_lease;
DROP INDEX IF EXISTS idx_v1469_paid_claim_events_claim_time;
DROP INDEX IF EXISTS idx_v1469_paid_claim_events_opportunity_time;
ALTER TABLE v1469_paid_execution_claim_events RENAME TO v1469_paid_execution_claim_events_018;
ALTER TABLE v1469_paid_execution_claims RENAME TO v1469_paid_execution_claims_018;
-- v1.4.69 single-winner paid-execution claim scaffold.
--
-- This migration grants no order-placement authority by itself.  It provides
-- the durable compare-and-swap boundary that a future, explicitly enabled
-- paid adapter must acquire before submitting an order for a market
-- opportunity.

CREATE TABLE IF NOT EXISTS v1469_paid_execution_claims (
    claim_id TEXT PRIMARY KEY,
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    arm_key TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'CLAIMED', 'SUBMITTING', 'UNKNOWN', 'SUBMITTED',
        'TERMINAL', 'ABANDONED'
    )),
    generation INTEGER NOT NULL CHECK(generation >= 1),
    claimed_at_ms INTEGER NOT NULL CHECK(claimed_at_ms >= 0),
    terminal_at_ms INTEGER CHECK(
        terminal_at_ms IS NULL OR terminal_at_ms >= claimed_at_ms
    ),
    terminal_reason TEXT,
    result_payload_json TEXT CHECK(
        result_payload_json IS NULL
        OR (
            json_valid(result_payload_json)
            AND length(result_payload_json) <= 4096
        )
    ),
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= claimed_at_ms),
    updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
    FOREIGN KEY(opportunity_id)
        REFERENCES v1469_market_opportunities(opportunity_id),
    FOREIGN KEY(arm_key) REFERENCES v1469_arm_leases(arm_key),
    FOREIGN KEY(lease_id) REFERENCES v1469_arm_leases(lease_id),
    UNIQUE(environment, symbol, opportunity_id),
    CHECK(
        (status IN ('CLAIMED', 'SUBMITTING', 'UNKNOWN', 'SUBMITTED')
            AND generation >= 1
            AND terminal_at_ms IS NULL
            AND terminal_reason IS NULL
            AND result_payload_json IS NULL)
        OR
        (status IN ('TERMINAL', 'ABANDONED')
            AND generation >= 2
            AND terminal_at_ms IS NOT NULL
            AND length(trim(terminal_reason)) > 0
            AND result_payload_json IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_v1469_paid_claim_status_time
ON v1469_paid_execution_claims(
    environment, symbol, status, updated_at_ms, claim_id
);

CREATE INDEX IF NOT EXISTS idx_v1469_paid_claim_arm_lease
ON v1469_paid_execution_claims(
    arm_key, lease_id, claimed_at_ms, claim_id
);

CREATE TRIGGER IF NOT EXISTS trg_v1469_paid_claim_opportunity_scope
BEFORE INSERT ON v1469_paid_execution_claims
WHEN NOT EXISTS (
    SELECT 1
    FROM v1469_market_opportunities AS opportunity
    WHERE opportunity.opportunity_id = NEW.opportunity_id
      AND opportunity.environment = NEW.environment
      AND opportunity.symbol = NEW.symbol
)
BEGIN
    SELECT RAISE(
        ABORT,
        'v1469 paid claim opportunity scope mismatch'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_v1469_paid_claim_active_lease
BEFORE INSERT ON v1469_paid_execution_claims
WHEN NOT EXISTS (
    SELECT 1
    FROM v1469_arm_leases AS lease
    WHERE lease.arm_key = NEW.arm_key
      AND lease.lease_id = NEW.lease_id
      AND lease.environment = NEW.environment
      AND lease.symbol = NEW.symbol
      AND lease.status = 'ACTIVE'
      AND lease.expires_at_ms > NEW.claimed_at_ms
)
BEGIN
    SELECT RAISE(
        ABORT,
        'v1469 paid claim requires matching active lease'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_v1469_paid_claim_no_delete
BEFORE DELETE ON v1469_paid_execution_claims
BEGIN
    SELECT RAISE(ABORT, 'v1469 paid claims cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_v1469_paid_claim_terminal_once
BEFORE UPDATE ON v1469_paid_execution_claims
WHEN OLD.status IN ('TERMINAL', 'ABANDONED')
BEGIN
    SELECT RAISE(ABORT, 'v1469 paid claim is already terminal');
END;

CREATE TRIGGER IF NOT EXISTS trg_v1469_paid_claim_transition_guard
BEFORE UPDATE ON v1469_paid_execution_claims
WHEN
    NEW.claim_id <> OLD.claim_id
    OR NEW.environment <> OLD.environment
    OR NEW.symbol <> OLD.symbol
    OR NEW.opportunity_id <> OLD.opportunity_id
    OR NEW.arm_key <> OLD.arm_key
    OR NEW.lease_id <> OLD.lease_id
    OR NEW.claimed_at_ms <> OLD.claimed_at_ms
    OR NEW.created_at_ms <> OLD.created_at_ms
    OR (OLD.status = 'CLAIMED' AND NEW.status NOT IN ('SUBMITTING', 'TERMINAL', 'ABANDONED'))
    OR (OLD.status = 'SUBMITTING' AND NEW.status NOT IN ('UNKNOWN', 'SUBMITTED', 'TERMINAL', 'ABANDONED'))
    OR (OLD.status = 'UNKNOWN' AND NEW.status NOT IN ('UNKNOWN', 'SUBMITTED', 'TERMINAL', 'ABANDONED'))
    OR (OLD.status = 'SUBMITTED' AND NEW.status NOT IN ('TERMINAL', 'ABANDONED'))
    OR NEW.generation <> OLD.generation + 1
    OR NEW.updated_at_ms < OLD.updated_at_ms
BEGIN
    SELECT RAISE(
        ABORT,
        'v1469 paid claim transition violates CAS contract'
    );
END;

CREATE TABLE IF NOT EXISTS v1469_paid_execution_claim_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE CHECK(
        length(trim(idempotency_key)) > 0
        AND length(idempotency_key) <= 256
    ),
    claim_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    arm_key TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    generation_before INTEGER NOT NULL CHECK(generation_before >= 0),
    generation_after INTEGER NOT NULL CHECK(
        generation_after = generation_before + 1
    ),
    event_time_ms INTEGER NOT NULL CHECK(event_time_ms >= 0),
    event_type TEXT NOT NULL CHECK(event_type IN (
        'CLAIMED', 'SUBMITTING', 'UNKNOWN', 'SUBMITTED',
        'TERMINAL', 'ABANDONED'
    )),
    actor TEXT NOT NULL CHECK(length(trim(actor)) > 0),
    payload_json TEXT NOT NULL CHECK(
        json_valid(payload_json) AND length(payload_json) <= 4096
    ),
    FOREIGN KEY(claim_id)
        REFERENCES v1469_paid_execution_claims(claim_id),
    FOREIGN KEY(opportunity_id)
        REFERENCES v1469_market_opportunities(opportunity_id),
    FOREIGN KEY(arm_key) REFERENCES v1469_arm_leases(arm_key),
    FOREIGN KEY(lease_id) REFERENCES v1469_arm_leases(lease_id)
);

CREATE INDEX IF NOT EXISTS idx_v1469_paid_claim_events_claim_time
ON v1469_paid_execution_claim_events(claim_id, event_time_ms, id);

CREATE INDEX IF NOT EXISTS idx_v1469_paid_claim_events_opportunity_time
ON v1469_paid_execution_claim_events(
    opportunity_id, event_time_ms, id
);

CREATE TRIGGER IF NOT EXISTS trg_v1469_paid_claim_event_identity
BEFORE INSERT ON v1469_paid_execution_claim_events
WHEN NOT EXISTS (
    SELECT 1
    FROM v1469_paid_execution_claims AS claim
    WHERE claim.claim_id = NEW.claim_id
      AND claim.opportunity_id = NEW.opportunity_id
      AND claim.arm_key = NEW.arm_key
      AND claim.lease_id = NEW.lease_id
      AND claim.generation = NEW.generation_after
      AND claim.status = NEW.event_type
      AND (
          (
              NEW.event_type = 'CLAIMED'
              AND claim.claimed_at_ms = NEW.event_time_ms
          )
          OR
          (
              NEW.event_type IN ('SUBMITTING', 'UNKNOWN', 'SUBMITTED')
              AND claim.updated_at_ms = NEW.event_time_ms
          )
          OR
          (
              NEW.event_type IN ('TERMINAL', 'ABANDONED')
              AND claim.terminal_at_ms = NEW.event_time_ms
          )
      )
)
BEGIN
    SELECT RAISE(
        ABORT,
        'v1469 paid claim event identity mismatch'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_v1469_paid_claim_events_no_update
BEFORE UPDATE ON v1469_paid_execution_claim_events
BEGIN
    SELECT RAISE(
        ABORT,
        'v1469 paid claim events are append-only'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_v1469_paid_claim_events_no_delete
BEFORE DELETE ON v1469_paid_execution_claim_events
BEGIN
    SELECT RAISE(
        ABORT,
        'v1469 paid claim events are append-only'
    );
END;

DROP TRIGGER trg_v1469_paid_claim_event_identity;
INSERT INTO v1469_paid_execution_claims SELECT * FROM v1469_paid_execution_claims_018;
INSERT INTO v1469_paid_execution_claim_events SELECT * FROM v1469_paid_execution_claim_events_018;
DROP TABLE v1469_paid_execution_claim_events_018;
DROP TABLE v1469_paid_execution_claims_018;
CREATE TRIGGER trg_v1469_paid_claim_event_identity
BEFORE INSERT ON v1469_paid_execution_claim_events
WHEN NOT EXISTS (
    SELECT 1 FROM v1469_paid_execution_claims AS claim
    WHERE claim.claim_id = NEW.claim_id
      AND claim.opportunity_id = NEW.opportunity_id
      AND claim.arm_key = NEW.arm_key AND claim.lease_id = NEW.lease_id
      AND claim.generation = NEW.generation_after AND claim.status = NEW.event_type
      AND ((NEW.event_type = 'CLAIMED' AND claim.claimed_at_ms = NEW.event_time_ms)
       OR (NEW.event_type IN ('SUBMITTING','UNKNOWN','SUBMITTED') AND claim.updated_at_ms = NEW.event_time_ms)
       OR (NEW.event_type IN ('TERMINAL','ABANDONED') AND claim.terminal_at_ms = NEW.event_time_ms))
)
BEGIN SELECT RAISE(ABORT, 'v1469 paid claim event identity mismatch'); END;
CREATE TRIGGER trg_v1469_paid_claim_event_requires_cid
BEFORE INSERT ON v1469_paid_execution_claim_events
WHEN NEW.event_type IN ('SUBMITTING', 'UNKNOWN', 'SUBMITTED')
 AND (json_type(NEW.payload_json, '$.client_order_id') <> 'text'
      OR length(trim(json_extract(NEW.payload_json, '$.client_order_id'))) = 0)
BEGIN
 SELECT RAISE(ABORT, 'v1469 submission event requires client_order_id');
END;
COMMIT;
PRAGMA foreign_keys = ON;
