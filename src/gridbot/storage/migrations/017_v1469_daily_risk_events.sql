-- v1.4.69 durable paid-close ledger for deterministic TPE active-day guards.
-- Rows are append-only.  The runtime rebuilds the current snapshot with the
-- pure v1469 risk reducer, so a restart cannot reset a loss/profit-lock latch.

CREATE TABLE IF NOT EXISTS v1469_daily_risk_events (
    event_id TEXT PRIMARY KEY,
    environment TEXT NOT NULL,
    symbol TEXT NOT NULL,
    active_day TEXT NOT NULL CHECK(
        length(active_day) = 10
        AND substr(active_day, 5, 1) = '-'
        AND substr(active_day, 8, 1) = '-'
    ),
    occurred_at_ms INTEGER NOT NULL CHECK(occurred_at_ms >= 0),
    event_type TEXT NOT NULL CHECK(event_type = 'PAID_CLOSED'),
    fee_net_pnl_delta_usdc REAL NOT NULL,
    risk_policy_hash TEXT NOT NULL CHECK(length(risk_policy_hash) = 64),
    source_run_id TEXT,
    source_trade_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK(
        json_valid(payload_json) AND length(payload_json) <= 2048
    ),
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= occurred_at_ms),
    UNIQUE(environment, symbol, source_trade_id)
);

CREATE INDEX IF NOT EXISTS idx_v1469_daily_risk_scope_day_time
ON v1469_daily_risk_events(
    environment, symbol, active_day, occurred_at_ms, event_id
);

CREATE TRIGGER IF NOT EXISTS trg_v1469_daily_risk_events_no_update
BEFORE UPDATE ON v1469_daily_risk_events
BEGIN
    SELECT RAISE(ABORT, 'v1469 daily risk events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_v1469_daily_risk_events_no_delete
BEFORE DELETE ON v1469_daily_risk_events
BEGIN
    SELECT RAISE(ABORT, 'v1469 daily risk events are append-only');
END;
