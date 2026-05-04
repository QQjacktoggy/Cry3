-- Migration 004: Add grid bot configuration columns to grid_sessions
-- These are populated by parsing the Binance share link sent via Telegram.
-- All columns are nullable since config is entered after session creation.

ALTER TABLE grid_sessions ADD COLUMN direction       TEXT;     -- NEUTRAL / LONG / SHORT
ALTER TABLE grid_sessions ADD COLUMN grid_type       TEXT;     -- GEO / ARITHMETIC
ALTER TABLE grid_sessions ADD COLUMN leverage        INTEGER;  -- e.g. 16
ALTER TABLE grid_sessions ADD COLUMN grid_count      INTEGER;  -- e.g. 50
ALTER TABLE grid_sessions ADD COLUMN lower_price     REAL;     -- lp
ALTER TABLE grid_sessions ADD COLUMN upper_price     REAL;     -- up
ALTER TABLE grid_sessions ADD COLUMN stop_loss_price REAL;     -- ssp
ALTER TABLE grid_sessions ADD COLUMN take_profit_price REAL;   -- stp
ALTER TABLE grid_sessions ADD COLUMN strategy_id     TEXT;     -- csi from share link
ALTER TABLE grid_sessions ADD COLUMN share_link      TEXT;     -- original URL
ALTER TABLE grid_sessions ADD COLUMN notified_close  INTEGER NOT NULL DEFAULT 0; -- 1 after close notification sent
