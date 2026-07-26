-- v1.4.69 compact, normalized Adaptive Arm observation foundation.
--
-- These tables are passive evidence storage.  They grant no order-placement,
-- cancellation, sizing, or execution authority.  Feature snapshots are stored
-- once per market opportunity; candidate and arm rows reference that durable
-- identity instead of repeating large classifier payloads.

CREATE TABLE IF NOT EXISTS v1469_market_opportunities (
    opportunity_id TEXT PRIMARY KEY,
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms >= 0),
    feature_at_ms INTEGER NOT NULL CHECK(
        feature_at_ms >= 0 AND feature_at_ms <= observed_at_ms
    ),
    coarse_regime TEXT NOT NULL CHECK(coarse_regime IN (
        'TREND_UP', 'TREND_DOWN', 'TREND',
        'RANGE', 'SHOCK', 'UNCERTAIN', 'UNKNOWN'
    )),
    regime_confidence REAL CHECK(
        regime_confidence IS NULL
        OR (regime_confidence >= 0 AND regime_confidence <= 1)
    ),
    feature_schema TEXT NOT NULL,
    feature_hash TEXT NOT NULL,
    feature_snapshot_json TEXT NOT NULL CHECK(
        json_valid(feature_snapshot_json)
        AND length(feature_snapshot_json) <= 32768
    ),
    source_run_id TEXT,
    source_event_id TEXT,
    data_quality TEXT NOT NULL CHECK(data_quality IN (
        'COMPLETE', 'DATA_INCOMPLETE'
    )),
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= observed_at_ms),
    UNIQUE(environment, symbol, source_event_id)
);

CREATE INDEX IF NOT EXISTS idx_v1469_opportunity_scope_time
ON v1469_market_opportunities(
    environment, symbol, observed_at_ms, opportunity_id
);

CREATE INDEX IF NOT EXISTS idx_v1469_opportunity_regime_time
ON v1469_market_opportunities(
    environment, symbol, coarse_regime, observed_at_ms, opportunity_id
);

CREATE TABLE IF NOT EXISTS v1469_lane_candidates (
    candidate_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    lane_code TEXT NOT NULL,
    effective_side TEXT NOT NULL CHECK(effective_side IN ('LONG', 'SHORT')),
    strategy TEXT NOT NULL,
    match_status TEXT NOT NULL CHECK(match_status IN (
        'MATCH', 'NEAR_MATCH', 'NO_MATCH'
    )),
    safety_status TEXT NOT NULL CHECK(safety_status IN (
        'SAFE', 'HARD_BLOCK', 'DATA_BLOCKED', 'NOT_EVALUATED'
    )),
    is_selected INTEGER NOT NULL DEFAULT 0 CHECK(is_selected IN (0, 1)),
    selection_rank INTEGER CHECK(selection_rank IS NULL OR selection_rank >= 0),
    suppression_reason TEXT,
    suppressed_by_lane_code TEXT,
    matcher_version TEXT NOT NULL,
    matcher_hash TEXT NOT NULL,
    data_complete INTEGER NOT NULL CHECK(data_complete IN (0, 1)),
    annotations_json TEXT NOT NULL DEFAULT '{}' CHECK(
        json_valid(annotations_json) AND length(annotations_json) <= 4096
    ),
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
    FOREIGN KEY(opportunity_id)
        REFERENCES v1469_market_opportunities(opportunity_id),
    UNIQUE(
        opportunity_id, lane_code, effective_side, strategy, matcher_hash
    ),
    CHECK(
        (match_status = 'MATCH')
        OR (is_selected = 0)
    )
);

CREATE INDEX IF NOT EXISTS idx_v1469_candidate_opportunity
ON v1469_lane_candidates(opportunity_id, match_status, candidate_id);

CREATE INDEX IF NOT EXISTS idx_v1469_candidate_lane_monitor
ON v1469_lane_candidates(
    lane_code, effective_side, match_status, safety_status, created_at_ms
);

CREATE INDEX IF NOT EXISTS idx_v1469_candidate_selected
ON v1469_lane_candidates(opportunity_id, is_selected, selection_rank)
WHERE is_selected = 1;

CREATE TABLE IF NOT EXISTS v1469_arm_evidence (
    evidence_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    arm_key TEXT NOT NULL,
    execution_profile_id TEXT NOT NULL,
    execution_profile_schema TEXT NOT NULL,
    execution_profile_hash TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('SHADOW', 'PAID')),
    diagnostic_only INTEGER NOT NULL DEFAULT 0
        CHECK(diagnostic_only IN (0, 1)),
    observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms >= 0),
    status TEXT NOT NULL CHECK(status IN (
        'PENDING', 'TERMINAL', 'DROPPED'
    )),
    terminal_at_ms INTEGER CHECK(
        terminal_at_ms IS NULL OR terminal_at_ms >= observed_at_ms
    ),
    outcome TEXT CHECK(outcome IS NULL OR outcome IN (
        'tp1_first', 'tp_first', 'tp',
        'sl_first', 'sl', 'max_hold', 'no_fill', 'ambiguous_both',
        'data_incomplete', 'dropped'
    )),
    fill_status TEXT CHECK(fill_status IS NULL OR fill_status IN (
        'FILLED', 'NO_FILL', 'UNKNOWN'
    )),
    data_complete INTEGER NOT NULL DEFAULT 0 CHECK(data_complete IN (0, 1)),
    ambiguous INTEGER NOT NULL DEFAULT 0 CHECK(ambiguous IN (0, 1)),
    reward_net_bp REAL,
    mfe_bp REAL,
    mae_bp REAL,
    terminal_reason TEXT,
    terminal_payload_json TEXT CHECK(
        terminal_payload_json IS NULL
        OR (
            json_valid(terminal_payload_json)
            AND length(terminal_payload_json) <= 4096
        )
    ),
    evidence_hash TEXT,
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= observed_at_ms),
    updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
    FOREIGN KEY(opportunity_id)
        REFERENCES v1469_market_opportunities(opportunity_id),
    FOREIGN KEY(candidate_id)
        REFERENCES v1469_lane_candidates(candidate_id),
    UNIQUE(
        opportunity_id, candidate_id, execution_profile_hash, source_type
    ),
    CHECK(
        (status = 'PENDING'
            AND terminal_at_ms IS NULL
            AND outcome IS NULL
            AND fill_status IS NULL
            AND evidence_hash IS NULL
            AND data_complete = 0
            AND ambiguous = 0)
        OR
        (status IN ('TERMINAL', 'DROPPED')
            AND terminal_at_ms IS NOT NULL
            AND outcome IS NOT NULL
            AND fill_status IS NOT NULL
            AND evidence_hash IS NOT NULL)
    ),
    CHECK(outcome <> 'ambiguous_both' OR ambiguous = 1),
    CHECK(status <> 'DROPPED' OR data_complete = 0),
    CHECK(outcome <> 'no_fill' OR fill_status = 'NO_FILL'),
    CHECK(
        outcome NOT IN (
            'tp1_first', 'tp_first', 'tp',
            'sl_first', 'sl', 'max_hold'
        )
        OR fill_status = 'FILLED'
    ),
    CHECK(
        outcome <> 'no_fill' OR COALESCE(reward_net_bp, 0) = 0
    )
);

CREATE INDEX IF NOT EXISTS idx_v1469_evidence_arm_window
ON v1469_arm_evidence(
    arm_key, observed_at_ms, terminal_at_ms, evidence_id
);

CREATE INDEX IF NOT EXISTS idx_v1469_evidence_candidate
ON v1469_arm_evidence(candidate_id, status, evidence_id);

CREATE INDEX IF NOT EXISTS idx_v1469_evidence_pending
ON v1469_arm_evidence(observed_at_ms, evidence_id)
WHERE status = 'PENDING';

CREATE TRIGGER IF NOT EXISTS trg_v1469_arm_evidence_no_delete
BEFORE DELETE ON v1469_arm_evidence
BEGIN
    SELECT RAISE(ABORT, 'v1469 arm evidence cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_v1469_arm_evidence_terminal_once
BEFORE UPDATE ON v1469_arm_evidence
WHEN OLD.status <> 'PENDING'
BEGIN
    SELECT RAISE(ABORT, 'v1469 arm evidence is already terminal');
END;

CREATE TRIGGER IF NOT EXISTS trg_v1469_arm_evidence_identity_immutable
BEFORE UPDATE ON v1469_arm_evidence
WHEN
    NEW.evidence_id <> OLD.evidence_id
    OR NEW.opportunity_id <> OLD.opportunity_id
    OR NEW.candidate_id <> OLD.candidate_id
    OR NEW.arm_key <> OLD.arm_key
    OR NEW.execution_profile_id <> OLD.execution_profile_id
    OR NEW.execution_profile_schema <> OLD.execution_profile_schema
    OR NEW.execution_profile_hash <> OLD.execution_profile_hash
    OR NEW.source_type <> OLD.source_type
    OR NEW.diagnostic_only <> OLD.diagnostic_only
    OR NEW.observed_at_ms <> OLD.observed_at_ms
    OR NEW.created_at_ms <> OLD.created_at_ms
    OR NEW.status NOT IN ('TERMINAL', 'DROPPED')
BEGIN
    SELECT RAISE(ABORT, 'v1469 arm evidence identity is immutable');
END;

CREATE TABLE IF NOT EXISTS v1469_arm_leases (
    arm_key TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL UNIQUE,
    generation INTEGER NOT NULL CHECK(generation >= 1),
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    lane_code TEXT NOT NULL,
    effective_side TEXT NOT NULL CHECK(effective_side IN ('LONG', 'SHORT')),
    strategy TEXT NOT NULL,
    coarse_regime TEXT NOT NULL CHECK(coarse_regime IN (
        'TREND_UP', 'TREND_DOWN', 'TREND',
        'RANGE', 'SHOCK', 'UNCERTAIN', 'UNKNOWN'
    )),
    execution_profile_id TEXT NOT NULL,
    execution_profile_schema TEXT NOT NULL,
    execution_profile_hash TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('PROBATION', 'LIVE')),
    status TEXT NOT NULL CHECK(status IN (
        'ACTIVE', 'COOLDOWN', 'DEMOTED', 'EXPIRED', 'REVOKED', 'HALTED'
    )),
    notional_cap_usdc REAL NOT NULL CHECK(
        notional_cap_usdc > 0
        AND notional_cap_usdc <= 50.0
        AND (phase <> 'PROBATION' OR notional_cap_usdc <= 25.0)
    ),
    risk_policy_hash TEXT NOT NULL,
    evidence_revision TEXT NOT NULL,
    evidence_as_of_ms INTEGER NOT NULL CHECK(evidence_as_of_ms >= 0),
    issued_at_ms INTEGER NOT NULL CHECK(issued_at_ms >= 0),
    renewed_at_ms INTEGER NOT NULL CHECK(renewed_at_ms >= issued_at_ms),
    expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms > renewed_at_ms),
    owner_id TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    demotion_reason TEXT,
    demoted_at_ms INTEGER,
    cooldown_until_ms INTEGER,
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
    CHECK(
        (status = 'ACTIVE'
            AND demotion_reason IS NULL
            AND demoted_at_ms IS NULL
            AND cooldown_until_ms IS NULL)
        OR
        (status = 'COOLDOWN'
            AND demotion_reason IS NOT NULL
            AND demoted_at_ms IS NOT NULL
            AND cooldown_until_ms > demoted_at_ms)
        OR
        (status NOT IN ('ACTIVE', 'COOLDOWN')
            AND demotion_reason IS NOT NULL
            AND demoted_at_ms IS NOT NULL
            AND cooldown_until_ms IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_v1469_one_active_arm_per_symbol
ON v1469_arm_leases(environment, symbol)
WHERE status = 'ACTIVE';

CREATE INDEX IF NOT EXISTS idx_v1469_lease_status_expiry
ON v1469_arm_leases(
    environment, symbol, status, expires_at_ms, arm_key
);

CREATE TABLE IF NOT EXISTS v1469_arm_events (
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
        'LEASE_RENEWED', 'LEASE_REVOKED', 'COOLDOWN',
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

CREATE INDEX IF NOT EXISTS idx_v1469_arm_events_arm_time
ON v1469_arm_events(arm_key, event_time_ms, id);

CREATE INDEX IF NOT EXISTS idx_v1469_arm_events_opportunity_time
ON v1469_arm_events(opportunity_id, event_time_ms, id);

CREATE TRIGGER IF NOT EXISTS trg_v1469_arm_events_no_update
BEFORE UPDATE ON v1469_arm_events
BEGIN
    SELECT RAISE(ABORT, 'v1469 arm events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_v1469_arm_events_no_delete
BEFORE DELETE ON v1469_arm_events
BEGIN
    SELECT RAISE(ABORT, 'v1469 arm events are append-only');
END;
