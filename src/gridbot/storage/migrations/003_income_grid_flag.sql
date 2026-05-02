-- Migration 003: Add is_grid_trade flag to income_records
-- Links income records to grid vs manual trades for accurate filtering.

ALTER TABLE income_records ADD COLUMN is_grid_trade INTEGER NOT NULL DEFAULT -1;
-- -1 = unknown (legacy rows), 1 = grid, 0 = manual

CREATE INDEX IF NOT EXISTS idx_income_grid ON income_records(is_grid_trade);
