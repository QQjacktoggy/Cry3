-- v1.4.63 startup reconciliation joins each started event to terminal events
-- from the same run.  The legacy (run_id, event_time_ms) and
-- (event_type, id) indexes each satisfy only half of that lookup and caused
-- the first scheduler cycle to exceed one minute on the live ledger.
CREATE INDEX IF NOT EXISTS idx_mainnet_run_events_run_type_id
ON mainnet_run_events(run_id, event_type, id);
