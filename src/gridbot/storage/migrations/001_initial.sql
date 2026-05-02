PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Note: grid_orders / grid_sub_orders were removed in the FAPI migration.
-- Tables created here are the ones that survive all migrations.

CREATE TABLE IF NOT EXISTS market_snapshots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol               TEXT NOT NULL,
    snapshot_time_ms     INTEGER NOT NULL,
    current_price        REAL NOT NULL,
    high_24h             REAL NOT NULL,
    low_24h              REAL NOT NULL,
    volume_24h           REAL NOT NULL,
    price_change_pct_24h REAL NOT NULL,
    funding_rate         REAL,
    next_funding_time_ms INTEGER,
    mark_price           REAL,
    klines_json          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_time ON market_snapshots(symbol, snapshot_time_ms);

CREATE TABLE IF NOT EXISTS performance_snapshots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                  TEXT NOT NULL,
    snapshot_time_ms        INTEGER NOT NULL,
    algo_id                 INTEGER,
    strategy_label          TEXT NOT NULL,
    realized_pnl            REAL NOT NULL,
    unrealized_pnl          REAL NOT NULL,
    funding_cost            REAL NOT NULL DEFAULT 0,
    fill_rate               REAL NOT NULL,
    price_range_utilization REAL NOT NULL,
    total_trades            INTEGER NOT NULL,
    leverage                INTEGER,
    liquidation_price       REAL,
    margin_ratio            REAL,
    apr_estimate            REAL,
    metrics_json            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_perf_symbol_time ON performance_snapshots(symbol, snapshot_time_ms);

CREATE TABLE IF NOT EXISTS recommendations (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at_ms               INTEGER NOT NULL,
    symbol                      TEXT,
    recommended_strategy        TEXT NOT NULL,
    confidence                  REAL NOT NULL,
    parameter_adjustments_json  TEXT NOT NULL,
    market_summary              TEXT NOT NULL,
    reasoning                   TEXT NOT NULL,
    risk_warnings_json          TEXT NOT NULL,
    trigger                     TEXT NOT NULL,
    acted_upon                  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_recommendations_time ON recommendations(created_at_ms);

CREATE TABLE IF NOT EXISTS strategy_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT NOT NULL,
    previous_strategy TEXT,
    new_strategy      TEXT NOT NULL,
    switch_reason     TEXT,
    switched_at_ms    INTEGER NOT NULL,
    recommendation_id INTEGER,
    FOREIGN KEY (recommendation_id) REFERENCES recommendations(id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time_ms   INTEGER NOT NULL,
    event_type      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    details_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(event_time_ms);

CREATE TABLE IF NOT EXISTS app_config (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
