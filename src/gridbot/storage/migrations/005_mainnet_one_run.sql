CREATE TABLE IF NOT EXISTS mainnet_runs (
    run_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    strategy_label TEXT NOT NULL,
    status TEXT NOT NULL,
    side TEXT,
    signal_json TEXT,
    params_json TEXT,
    entry_order_id INTEGER,
    entry_client_order_id TEXT,
    entry_price REAL,
    avg_entry_price REAL,
    qty REAL DEFAULT 0,
    cumulative_notional_usdc REAL DEFAULT 0,
    realized_pnl_usdc REAL DEFAULT 0,
    commission_usdc REAL DEFAULT 0,
    exit_reason TEXT,
    error TEXT,
    armed_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    completed_at_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_mainnet_runs_status_updated
ON mainnet_runs(status, updated_at_ms);

CREATE TABLE IF NOT EXISTS mainnet_run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_time_ms INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES mainnet_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_mainnet_run_events_run_time
ON mainnet_run_events(run_id, event_time_ms);
