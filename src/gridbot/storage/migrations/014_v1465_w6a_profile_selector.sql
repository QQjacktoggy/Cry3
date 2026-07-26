-- v1.4.65 W6A resolved-profile evidence and winner selector.
-- Evidence is immutable.  A selector is the single mutable authority for one
-- W6A cohort; every transition is recorded in the append-only event ledger.

CREATE TABLE IF NOT EXISTS v1465_w6a_profile_evidence (
    evidence_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    lane_code TEXT NOT NULL CHECK(lane_code = 'W6A'),
    market_state TEXT NOT NULL,
    effective_side TEXT NOT NULL CHECK(effective_side IN ('LONG', 'SHORT')),
    strategy TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    resolved_profile_hash TEXT NOT NULL,
    profile_plan_hash TEXT NOT NULL,
    observed_at_ms INTEGER NOT NULL CHECK(observed_at_ms >= 0),
    terminal_at_ms INTEGER NOT NULL CHECK(terminal_at_ms >= observed_at_ms),
    outcome TEXT NOT NULL CHECK(outcome IN (
        'tp1_first', 'tp_first', 'tp', 'sl_first', 'sl', 'max_hold',
        'no_fill', 'ambiguous_both'
    )),
    data_complete INTEGER NOT NULL CHECK(data_complete IN (0, 1)),
    ambiguous INTEGER NOT NULL CHECK(ambiguous IN (0, 1)),
    diagnostic_only INTEGER NOT NULL DEFAULT 0
        CHECK(diagnostic_only IN (0, 1)),
    net_pnl_bp REAL,
    source_payload_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= terminal_at_ms),
    CHECK(outcome <> 'ambiguous_both' OR ambiguous = 1),
    UNIQUE(opportunity_id, resolved_profile_hash)
);

CREATE INDEX IF NOT EXISTS idx_v1465_w6a_evidence_selector_window
ON v1465_w6a_profile_evidence(
    environment, symbol, lane_code, market_state, effective_side, strategy,
    resolved_profile_hash, observed_at_ms, terminal_at_ms, evidence_id
);

CREATE INDEX IF NOT EXISTS idx_v1465_w6a_evidence_terminal_window
ON v1465_w6a_profile_evidence(terminal_at_ms, opportunity_id, resolved_profile_hash);

CREATE TRIGGER IF NOT EXISTS trg_v1465_w6a_profile_evidence_no_update
BEFORE UPDATE ON v1465_w6a_profile_evidence
BEGIN
    SELECT RAISE(ABORT, 'v1465 W6A profile evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_v1465_w6a_profile_evidence_no_delete
BEFORE DELETE ON v1465_w6a_profile_evidence
BEGIN
    SELECT RAISE(ABORT, 'v1465 W6A profile evidence is immutable');
END;

CREATE TABLE IF NOT EXISTS v1465_w6a_profile_selections (
    selector_key TEXT PRIMARY KEY,
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    lane_code TEXT NOT NULL CHECK(lane_code = 'W6A'),
    market_state TEXT NOT NULL,
    effective_side TEXT NOT NULL CHECK(effective_side IN ('LONG', 'SHORT')),
    strategy TEXT NOT NULL,
    winner_profile_id TEXT NOT NULL,
    winner_resolved_profile_hash TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation >= 1),
    status TEXT NOT NULL CHECK(status IN (
        'SHADOW', 'PROBATION', 'LIVE', 'EXPIRED', 'DEMOTED'
    )),
    notional_cap_usdc REAL NOT NULL CHECK(notional_cap_usdc >= 0),
    issued_at_ms INTEGER NOT NULL CHECK(issued_at_ms >= 0),
    renewed_at_ms INTEGER NOT NULL CHECK(renewed_at_ms >= issued_at_ms),
    expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms > renewed_at_ms),
    evidence_revision TEXT NOT NULL,
    evidence_snapshot_hash TEXT NOT NULL,
    evidence_snapshot_json TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    demotion_reason TEXT,
    demoted_at_ms INTEGER,
    cooldown_until_ms INTEGER,
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
    CHECK(
        (status IN ('SHADOW', 'PROBATION', 'LIVE')
            AND demotion_reason IS NULL AND demoted_at_ms IS NULL
            AND cooldown_until_ms IS NULL)
        OR
        (status = 'EXPIRED' AND demotion_reason = 'selector_expired'
            AND demoted_at_ms IS NOT NULL AND cooldown_until_ms IS NULL)
        OR
        (status = 'DEMOTED' AND demotion_reason IS NOT NULL
            AND demoted_at_ms IS NOT NULL
            AND (cooldown_until_ms IS NULL OR cooldown_until_ms > demoted_at_ms))
    ),
    CHECK(status NOT IN ('PROBATION', 'LIVE') OR notional_cap_usdc > 0)
);

CREATE INDEX IF NOT EXISTS idx_v1465_w6a_selection_status_expiry
ON v1465_w6a_profile_selections(status, expires_at_ms, selector_key);

CREATE TABLE IF NOT EXISTS v1465_w6a_profile_selection_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    selector_key TEXT NOT NULL,
    generation_before INTEGER CHECK(generation_before IS NULL OR generation_before >= 0),
    generation_after INTEGER CHECK(generation_after IS NULL OR generation_after >= 1),
    event_time_ms INTEGER NOT NULL CHECK(event_time_ms >= 0),
    event_type TEXT NOT NULL CHECK(event_type IN (
        'GRANTED', 'RENEWED', 'SWITCHED', 'DEMOTED', 'EXPIRED'
    )),
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    CHECK(generation_after IS NULL OR generation_before IS NULL
          OR generation_after > generation_before)
);

CREATE INDEX IF NOT EXISTS idx_v1465_w6a_selection_events_selector_time
ON v1465_w6a_profile_selection_events(selector_key, event_time_ms, id);

CREATE TRIGGER IF NOT EXISTS trg_v1465_w6a_selection_events_no_update
BEFORE UPDATE ON v1465_w6a_profile_selection_events
BEGIN
    SELECT RAISE(ABORT, 'v1465 W6A selection events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_v1465_w6a_selection_events_no_delete
BEFORE DELETE ON v1465_w6a_profile_selection_events
BEGIN
    SELECT RAISE(ABORT, 'v1465 W6A selection events are append-only');
END;
