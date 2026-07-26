-- Bound v1.4.65 ledger reconciliation to the tiny set of W6A profile rows.
-- The generic event ledger already contains hundreds of thousands of rows;
-- JSON scans here would otherwise block the ten-second management cycle.
CREATE INDEX IF NOT EXISTS idx_v1465_w6a_base_start_intent
ON mainnet_run_events(
    json_extract(details_json, '$.version'),
    id
)
WHERE event_type = 'entry_codex_v1_shadow_sample_started'
  AND COALESCE(
        json_extract(details_json, '$.v1465_profile_evidence'),
        0
      ) <> 1
  AND UPPER(
        COALESCE(
            json_extract(details_json, '$.lane_code'),
            json_extract(details_json, '$.effective_lane'),
            json_extract(details_json, '$.shadow_lane'),
            ''
        )
      ) = 'W6A';

CREATE INDEX IF NOT EXISTS idx_v1465_w6a_profile_start_group
ON mainnet_run_events(
    run_id,
    json_extract(details_json, '$.v1465_opportunity_id'),
    json_extract(details_json, '$.sample_id'),
    id
)
WHERE event_type = 'entry_codex_v1_shadow_sample_started'
  AND json_extract(details_json, '$.v1465_profile_evidence') = 1;

CREATE INDEX IF NOT EXISTS idx_v1465_w6a_terminal_outcome
ON mainnet_run_events(
    json_extract(details_json, '$.v1465_profile_evidence'),
    id
)
WHERE event_type = 'entry_codex_v1_shadow_outcome';

CREATE INDEX IF NOT EXISTS idx_v1465_w6a_projection_ack
ON mainnet_run_events(
    run_id,
    json_extract(details_json, '$.sample_id'),
    json_extract(details_json, '$.v1465_opportunity_id'),
    json_extract(details_json, '$.v1465_profile_id'),
    json_extract(details_json, '$.v1465_resolved_profile_hash'),
    id
)
WHERE event_type = 'entry_codex_v1465_profile_evidence_projected';
