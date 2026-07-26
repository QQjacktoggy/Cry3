-- v1.4.64 adaptive-promotion persistence.
--
-- Promotion evidence is an immutable, normalized projection of one terminal
-- shadow opportunity.  Runtime leases are current state; every state change is
-- paired with an append-only event by the repository in one transaction.

CREATE TABLE IF NOT EXISTS v1464_promotion_evidence (
    opportunity_id TEXT PRIMARY KEY,
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    lane_code TEXT NOT NULL,
    market_state TEXT NOT NULL,
    effective_side TEXT NOT NULL
        CHECK(effective_side IN ('LONG', 'SHORT')),
    strategy TEXT NOT NULL,
    resolved_profile_hash TEXT NOT NULL,
    profile_identity_schema TEXT NOT NULL
        CHECK(profile_identity_schema = 'v1464.stable-profile.1'),
    registry_version TEXT NOT NULL,
    registry_hash TEXT NOT NULL,
    lane_definition_hash TEXT NOT NULL,
    admission_policy_hash TEXT NOT NULL,
    evidence_schema_version TEXT NOT NULL
        CHECK(evidence_schema_version = 'v1464.sliding-evidence.1'),
    observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms >= 0),
    terminal_at_ms INTEGER NOT NULL
        CHECK(terminal_at_ms >= observed_at_ms),
    outcome TEXT NOT NULL CHECK(outcome IN (
        'tp1_first', 'tp_first', 'tp',
        'sl_first', 'sl', 'max_hold', 'no_fill', 'ambiguous_both'
    )),
    data_complete INTEGER NOT NULL
        CHECK(data_complete IN (0, 1)),
    ambiguous INTEGER NOT NULL
        CHECK(ambiguous IN (0, 1)),
    diagnostic_only INTEGER NOT NULL DEFAULT 0
        CHECK(diagnostic_only IN (0, 1)),
    net_pnl_usdc REAL,
    source_type TEXT NOT NULL CHECK(source_type IN (
        'SHADOW', 'SHADOW_DROP', 'PAID'
    )),
    source_id TEXT NOT NULL,
    source_payload_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= terminal_at_ms),
    CHECK(outcome <> 'ambiguous_both' OR ambiguous = 1),
    UNIQUE(source_type, source_id)
);

CREATE INDEX IF NOT EXISTS idx_v1464_promotion_evidence_cohort_window
ON v1464_promotion_evidence(
    environment,
    symbol,
    lane_code,
    market_state,
    effective_side,
    strategy,
    resolved_profile_hash,
    profile_identity_schema,
    registry_version,
    registry_hash,
    lane_definition_hash,
    admission_policy_hash,
    observed_at_ms,
    terminal_at_ms
);

CREATE INDEX IF NOT EXISTS idx_v1464_promotion_evidence_terminal
ON v1464_promotion_evidence(terminal_at_ms, opportunity_id);

CREATE INDEX IF NOT EXISTS idx_v1464_promotion_evidence_lane_paid_window
ON v1464_promotion_evidence(
    environment,
    symbol,
    lane_code,
    source_type,
    evidence_schema_version,
    observed_at_ms,
    terminal_at_ms
);

CREATE TABLE IF NOT EXISTS v1464_lane_promotion_leases (
    cohort_key TEXT PRIMARY KEY,
    lease_id TEXT NOT NULL UNIQUE,
    generation INTEGER NOT NULL CHECK(generation >= 1),
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    lane_code TEXT NOT NULL,
    market_state TEXT NOT NULL,
    effective_side TEXT NOT NULL
        CHECK(effective_side IN ('LONG', 'SHORT')),
    strategy TEXT NOT NULL,
    resolved_profile_hash TEXT NOT NULL,
    profile_identity_schema TEXT NOT NULL
        CHECK(profile_identity_schema = 'v1464.stable-profile.1'),
    registry_version TEXT NOT NULL,
    registry_hash TEXT NOT NULL,
    lane_definition_hash TEXT NOT NULL,
    admission_policy_hash TEXT NOT NULL,
    promotion_policy_hash TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN ('PROBATION', 'CONTROL')),
    status TEXT NOT NULL CHECK(status IN (
        'ACTIVE', 'COOLDOWN', 'DEMOTED', 'EXPIRED', 'REVOKED', 'HALTED'
    )),
    notional_cap_usdc REAL NOT NULL CHECK(
        notional_cap_usdc > 0
        AND notional_cap_usdc <= 50.0
        AND (phase <> 'PROBATION' OR notional_cap_usdc <= 25.0)
    ),
    evidence_window_start_ms INTEGER NOT NULL
        CHECK(evidence_window_start_ms >= 0),
    evidence_as_of_ms INTEGER NOT NULL
        CHECK(evidence_as_of_ms >= evidence_window_start_ms),
    evidence_watermark INTEGER NOT NULL DEFAULT 0
        CHECK(evidence_watermark >= 0),
    evidence_snapshot_hash TEXT NOT NULL,
    evidence_snapshot_json TEXT NOT NULL,
    issued_at_ms INTEGER NOT NULL CHECK(issued_at_ms >= 0),
    renewed_at_ms INTEGER NOT NULL CHECK(renewed_at_ms >= issued_at_ms),
    expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms > renewed_at_ms),
    boot_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    soft_failures INTEGER NOT NULL DEFAULT 0 CHECK(soft_failures >= 0),
    demotion_reason TEXT,
    demoted_at_ms INTEGER,
    cooldown_until_ms INTEGER,
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
    CHECK(
        (
            status = 'ACTIVE'
            AND demotion_reason IS NULL
            AND demoted_at_ms IS NULL
            AND cooldown_until_ms IS NULL
        )
        OR
        (
            status = 'COOLDOWN'
            AND demotion_reason IS NOT NULL
            AND demoted_at_ms IS NOT NULL
            AND cooldown_until_ms > demoted_at_ms
        )
        OR
        (
            status NOT IN ('ACTIVE', 'COOLDOWN')
            AND demotion_reason IS NOT NULL
            AND demoted_at_ms IS NOT NULL
            AND cooldown_until_ms IS NULL
        )
    ),
    CHECK(cooldown_until_ms IS NULL OR cooldown_until_ms >= 0)
);

CREATE INDEX IF NOT EXISTS idx_v1464_lane_promotion_leases_status_expiry
ON v1464_lane_promotion_leases(status, expires_at_ms);

CREATE INDEX IF NOT EXISTS idx_v1464_lane_promotion_leases_identity
ON v1464_lane_promotion_leases(
    environment,
    symbol,
    lane_code,
    market_state,
    effective_side,
    strategy,
    resolved_profile_hash,
    profile_identity_schema,
    registry_version,
    registry_hash,
    lane_definition_hash,
    admission_policy_hash,
    promotion_policy_hash
);

CREATE TABLE IF NOT EXISTS v1464_lane_promotion_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    cohort_key TEXT NOT NULL,
    lease_id TEXT,
    generation_before INTEGER CHECK(
        generation_before IS NULL OR generation_before >= 0
    ),
    generation_after INTEGER CHECK(
        generation_after IS NULL OR generation_after >= 1
    ),
    event_time_ms INTEGER NOT NULL CHECK(event_time_ms >= 0),
    event_type TEXT NOT NULL CHECK(event_type IN (
        'EVALUATED',
        'PROBATION_GRANTED',
        'LEASE_RENEWED',
        'CONTROL_GRANTED',
        'COOLDOWN',
        'DEMOTED',
        'EXPIRED',
        'REVOKED',
        'ADMISSION_CONSUMED',
        'ADMISSION_BLOCKED',
        'HALTED'
    )),
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    CHECK(
        generation_after IS NULL
        OR generation_before IS NULL
        OR generation_after > generation_before
    )
);

CREATE INDEX IF NOT EXISTS idx_v1464_lane_promotion_events_lease_time
ON v1464_lane_promotion_events(lease_id, event_time_ms, id);

CREATE INDEX IF NOT EXISTS idx_v1464_lane_promotion_events_cohort_time
ON v1464_lane_promotion_events(cohort_key, event_time_ms, id);

CREATE TRIGGER IF NOT EXISTS trg_v1464_promotion_events_no_update
BEFORE UPDATE ON v1464_lane_promotion_events
BEGIN
    SELECT RAISE(ABORT, 'v1464 promotion events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_v1464_promotion_events_no_delete
BEFORE DELETE ON v1464_lane_promotion_events
BEGIN
    SELECT RAISE(ABORT, 'v1464 promotion events are append-only');
END;
