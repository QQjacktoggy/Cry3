-- Freeze pre-v1.4.62 event-only shadow history into a compact informational
-- summary.  Promotion readiness reads only events after this immutable cutoff;
-- legacy rows remain visible but cannot contaminate the new exact cohort.
CREATE TABLE IF NOT EXISTS v1462_lane_monitor_legacy_summary (
    lane_code TEXT PRIMARY KEY,
    outcome_opportunities INTEGER NOT NULL,
    last_outcome_at_ms INTEGER NOT NULL,
    snapshot_max_event_id INTEGER NOT NULL,
    snapshot_at_ms INTEGER NOT NULL
);

INSERT OR IGNORE INTO v1462_lane_monitor_legacy_summary (
    lane_code,
    outcome_opportunities,
    last_outcome_at_ms,
    snapshot_max_event_id,
    snapshot_at_ms
)
WITH snapshot AS (
    SELECT
        COALESCE(MAX(id), 0) AS max_event_id,
        COALESCE(MAX(event_time_ms), 0) AS snapshot_at_ms
    FROM mainnet_run_events
),
normalized AS (
    SELECT
        UPPER(TRIM(COALESCE(
            json_extract(details_json, '$.legacy_lane_code'),
            json_extract(details_json, '$.lane_code'),
            json_extract(details_json, '$.candidate_lane'),
            json_extract(details_json, '$.shadow_lane'),
            ''
        ))) AS lane_code,
        COALESCE(
            json_extract(details_json, '$.v1462_opportunity_id'),
            json_extract(details_json, '$.opportunity_id'),
            json_extract(details_json, '$.strict_sample_id'),
            json_extract(details_json, '$.sample_id'),
            run_id || ':' || id
        ) AS opportunity_id,
        event_time_ms
    FROM mainnet_run_events
    WHERE event_type = 'entry_codex_v1_shadow_outcome'
)
SELECT
    normalized.lane_code,
    COUNT(DISTINCT normalized.opportunity_id),
    MAX(normalized.event_time_ms),
    snapshot.max_event_id,
    snapshot.snapshot_at_ms
FROM normalized
CROSS JOIN snapshot
WHERE normalized.lane_code <> ''
GROUP BY normalized.lane_code;
