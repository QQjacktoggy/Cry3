-- Exact, outcome-blind opportunity evidence for the v1.4.58 continuation.
-- Existing v1 rows remain readable with NULL v2 columns. New runtime writes
-- must satisfy the v2 repository contract before they can enter promotion
-- denominators. This migration is observational and grants no order capability.

ALTER TABLE adaptive_opportunities ADD COLUMN source_run_id TEXT;
ALTER TABLE adaptive_opportunities ADD COLUMN opportunity_bucket INTEGER
    CHECK(opportunity_bucket IS NULL OR opportunity_bucket >= 0);
ALTER TABLE adaptive_opportunities ADD COLUMN decision_at_ms INTEGER
    CHECK(decision_at_ms IS NULL OR decision_at_ms >= 0);
ALTER TABLE adaptive_opportunities ADD COLUMN feature_snapshot_json TEXT;
ALTER TABLE adaptive_opportunities ADD COLUMN feature_timestamps_json TEXT;
ALTER TABLE adaptive_opportunities ADD COLUMN evidence_contract_version TEXT;
ALTER TABLE adaptive_opportunities ADD COLUMN outcome_blind INTEGER
    CHECK(outcome_blind IS NULL OR outcome_blind IN (0, 1));

CREATE INDEX IF NOT EXISTS idx_adaptive_opportunities_exact_join
ON adaptive_opportunities(session_id, source_run_id, opportunity_id);

CREATE INDEX IF NOT EXISTS idx_adaptive_opportunities_bucket
ON adaptive_opportunities(
    session_id,
    opportunity_bucket,
    decision_at_ms,
    opportunity_id
);
