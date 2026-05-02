-- Migration 002: Switch from grid_orders/sub_orders to futures_trades/income_records
-- This migration adds new tables and drops the old ones.

PRAGMA foreign_keys = OFF;

-- Drop old grid-specific tables that relied on SAPI algo endpoints
DROP TABLE IF EXISTS grid_sub_orders;
DROP TABLE IF EXISTS grid_orders;

-- Futures trades (from /fapi/v1/userTrades)
CREATE TABLE IF NOT EXISTS futures_trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id         INTEGER UNIQUE NOT NULL,
    order_id         INTEGER NOT NULL,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,           -- BUY / SELL
    price            REAL NOT NULL,
    qty              REAL NOT NULL,
    quote_qty        REAL NOT NULL,
    realized_pnl     REAL NOT NULL,
    commission       REAL NOT NULL,
    commission_asset TEXT NOT NULL,
    time_ms          INTEGER NOT NULL,
    position_side    TEXT NOT NULL,           -- BOTH / LONG / SHORT
    is_maker         INTEGER NOT NULL,        -- 1=maker, 0=taker
    is_grid_trade    INTEGER NOT NULL DEFAULT 1,  -- 1=grid bot, 0=manual
    fetched_at_ms    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_time ON futures_trades(symbol, time_ms);
CREATE INDEX IF NOT EXISTS idx_trades_order_id ON futures_trades(order_id);

-- Income records (from /fapi/v1/income)
CREATE TABLE IF NOT EXISTS income_records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tran_id      INTEGER UNIQUE NOT NULL,
    symbol       TEXT,                        -- may be empty for STRATEGY_UMFUTURES_TRANSFER
    income_type  TEXT NOT NULL,               -- REALIZED_PNL, COMMISSION, FUNDING_FEE, STRATEGY_UMFUTURES_TRANSFER, etc.
    income       REAL NOT NULL,
    asset        TEXT NOT NULL,
    time_ms      INTEGER NOT NULL,
    info         TEXT,                        -- tradeId, "FUNDING_FEE", "UM_GRID_CREATE", "UM_GRID_CLOSE"
    trade_id     TEXT,                        -- may be empty
    fetched_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_income_type_time ON income_records(income_type, time_ms);
CREATE INDEX IF NOT EXISTS idx_income_symbol ON income_records(symbol, time_ms);

-- Grid sessions (paired STRATEGY_UMFUTURES_TRANSFER CREATE/CLOSE)
CREATE TABLE IF NOT EXISTS grid_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol           TEXT,                    -- inferred from trades in time window
    created_at_ms    INTEGER NOT NULL,
    closed_at_ms     INTEGER,                -- NULL if still running
    invested_amount  REAL NOT NULL,           -- absolute CREATE transfer amount
    returned_amount  REAL,                    -- CLOSE transfer amount
    net_profit       REAL,                    -- returned - invested
    asset            TEXT NOT NULL,
    create_tran_id   INTEGER UNIQUE NOT NULL,
    close_tran_id    INTEGER UNIQUE,
    is_active        INTEGER DEFAULT 1        -- 1=running, 0=closed
);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON grid_sessions(is_active);
CREATE INDEX IF NOT EXISTS idx_sessions_symbol ON grid_sessions(symbol);

PRAGMA foreign_keys = ON;
