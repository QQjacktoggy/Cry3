-- Durable evidence layer for v1.4.59.  This migration is observational:
-- no table here authorises order placement, cancellation, or risk mutation.

CREATE TABLE IF NOT EXISTS adaptive_sessions (
    session_id TEXT PRIMARY KEY,
    environment TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    database_identity TEXT NOT NULL,
    exchange_endpoint TEXT NOT NULL,
    is_testnet INTEGER NOT NULL CHECK(is_testnet IN (0, 1)),
    symbol TEXT NOT NULL,
    account_mode TEXT NOT NULL,
    deployment_commit TEXT NOT NULL,
    code_version TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL,
    last_checkpoint_at_ms INTEGER NOT NULL,
    stopped_at_ms INTEGER,
    terminal_runs INTEGER NOT NULL DEFAULT 0 CHECK(terminal_runs >= 0),
    gross_pnl_usdc REAL NOT NULL DEFAULT 0,
    commission_usdc REAL NOT NULL DEFAULT 0,
    funding_usdc REAL NOT NULL DEFAULT 0,
    net_pnl_usdc REAL NOT NULL DEFAULT 0,
    high_water_net_pnl_usdc REAL NOT NULL DEFAULT 0,
    rearm_pending INTEGER NOT NULL DEFAULT 0 CHECK(rearm_pending IN (0, 1)),
    pause_reason TEXT,
    stop_reason TEXT,
    counters_json TEXT NOT NULL,
    disabled_states_json TEXT NOT NULL,
    route_stats_json TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 0),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_adaptive_sessions_one_open_scope
ON adaptive_sessions(environment, account_fingerprint, database_identity, symbol)
WHERE status IN ('ACTIVE', 'PAUSED_REQUIRES_ACK');

CREATE INDEX IF NOT EXISTS idx_adaptive_sessions_scope_checkpoint
ON adaptive_sessions(
    environment,
    account_fingerprint,
    database_identity,
    symbol,
    status,
    last_checkpoint_at_ms DESC
);

CREATE TABLE IF NOT EXISTS adaptive_opportunities (
    session_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    observed_at_ms INTEGER NOT NULL,
    feature_hash TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    lane_code TEXT NOT NULL,
    market_state TEXT NOT NULL,
    reject_reason TEXT,
    promotion_source TEXT,
    decision_schema_version TEXT NOT NULL,
    action_schema_json TEXT NOT NULL,
    raw_decision_json TEXT NOT NULL,
    effective_decision_json TEXT NOT NULL,
    quality_status TEXT NOT NULL,
    recorded_at_ms INTEGER NOT NULL,
    PRIMARY KEY(session_id, opportunity_id),
    FOREIGN KEY(session_id) REFERENCES adaptive_sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_adaptive_opportunities_session_time
ON adaptive_opportunities(session_id, observed_at_ms, opportunity_id);

CREATE INDEX IF NOT EXISTS idx_adaptive_opportunities_session_quality
ON adaptive_opportunities(session_id, quality_status, observed_at_ms);

CREATE TABLE IF NOT EXISTS shadow_evaluations (
    session_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    variant TEXT NOT NULL,
    fill_model TEXT NOT NULL,
    simulation_version TEXT NOT NULL,
    entry_offset_bp REAL NOT NULL,
    entry_limit_price REAL,
    decision_latency_ms INTEGER NOT NULL,
    entry_ttl_ms INTEGER NOT NULL,
    fill_status TEXT NOT NULL,
    filled_qty REAL NOT NULL DEFAULT 0,
    avg_fill_price REAL,
    first_fill_at_ms INTEGER,
    fill_age_ms INTEGER,
    partial_fill_ratio REAL NOT NULL DEFAULT 0,
    tp_anchor TEXT,
    tp_bp REAL,
    sl_anchor TEXT,
    sl_bp REAL,
    max_hold_ms INTEGER,
    mfe_bp REAL,
    mae_bp REAL,
    exit_at_ms INTEGER,
    exit_price REAL,
    exit_reason TEXT,
    gross_pnl_usdc REAL,
    commission_usdc REAL,
    funding_usdc REAL,
    net_pnl_usdc REAL,
    data_quality TEXT NOT NULL,
    ambiguous_touch INTEGER NOT NULL DEFAULT 0 CHECK(ambiguous_touch IN (0, 1)),
    input_json TEXT NOT NULL,
    recorded_at_ms INTEGER NOT NULL,
    PRIMARY KEY(session_id, opportunity_id, variant, fill_model, simulation_version),
    FOREIGN KEY(session_id, opportunity_id)
        REFERENCES adaptive_opportunities(session_id, opportunity_id)
);

CREATE INDEX IF NOT EXISTS idx_shadow_evaluations_session_variant
ON shadow_evaluations(session_id, variant, fill_model, data_quality, recorded_at_ms);

CREATE TABLE IF NOT EXISTS run_reconciliations (
    run_id TEXT NOT NULL,
    reconciliation_revision INTEGER NOT NULL CHECK(reconciliation_revision >= 0),
    environment TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    reconciliation_status TEXT NOT NULL CHECK(reconciliation_status IN ('COMPLETE', 'DATA_INCOMPLETE')),
    completeness_reason TEXT,
    gross_realized_pnl_usdc REAL NOT NULL DEFAULT 0,
    commission_usdc REAL,
    funding_usdc REAL,
    net_pnl_usdc REAL,
    entry_maker_fills INTEGER NOT NULL DEFAULT 0 CHECK(entry_maker_fills >= 0),
    entry_taker_fills INTEGER NOT NULL DEFAULT 0 CHECK(entry_taker_fills >= 0),
    exit_maker_fills INTEGER NOT NULL DEFAULT 0 CHECK(exit_maker_fills >= 0),
    exit_taker_fills INTEGER NOT NULL DEFAULT 0 CHECK(exit_taker_fills >= 0),
    source_json TEXT NOT NULL,
    reconciled_at_ms INTEGER NOT NULL,
    PRIMARY KEY(run_id, reconciliation_revision),
    FOREIGN KEY(run_id) REFERENCES mainnet_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_run_reconciliations_status_time
ON run_reconciliations(reconciliation_status, reconciled_at_ms DESC);

CREATE TABLE IF NOT EXISTS run_reconciliation_exchange_trades (
    environment TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    exchange_trade_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    reconciliation_revision INTEGER NOT NULL,
    order_id TEXT,
    role TEXT NOT NULL,
    is_maker INTEGER CHECK(is_maker IN (0, 1)),
    realized_pnl_usdc REAL,
    commission_amount REAL,
    commission_asset TEXT,
    commission_usdc REAL,
    source_json TEXT NOT NULL,
    PRIMARY KEY(environment, account_fingerprint, exchange_trade_id),
    FOREIGN KEY(run_id, reconciliation_revision)
        REFERENCES run_reconciliations(run_id, reconciliation_revision)
);

CREATE TABLE IF NOT EXISTS run_reconciliation_exchange_income (
    environment TEXT NOT NULL,
    account_fingerprint TEXT NOT NULL,
    exchange_income_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    reconciliation_revision INTEGER NOT NULL,
    income_type TEXT NOT NULL,
    amount REAL NOT NULL,
    asset TEXT NOT NULL,
    amount_usdc REAL,
    source_json TEXT NOT NULL,
    PRIMARY KEY(environment, account_fingerprint, exchange_income_id),
    FOREIGN KEY(run_id, reconciliation_revision)
        REFERENCES run_reconciliations(run_id, reconciliation_revision)
);

CREATE INDEX IF NOT EXISTS idx_reconciliation_trades_run
ON run_reconciliation_exchange_trades(run_id, reconciliation_revision);

CREATE INDEX IF NOT EXISTS idx_reconciliation_income_run
ON run_reconciliation_exchange_income(run_id, reconciliation_revision);
