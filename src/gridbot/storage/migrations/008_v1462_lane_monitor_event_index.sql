-- Keep the read-only v1.4.62 Lane Monitor interactive as the append-only
-- event ledger grows.  The monitor filters by these event types and then
-- orders by id, so this index avoids a full scan of the historical ledger.
CREATE INDEX IF NOT EXISTS idx_mainnet_run_events_event_type_id
ON mainnet_run_events(event_type, id DESC);
