-- A singleton cutoff is required even when the legacy snapshot contains no
-- per-lane outcome rows.  Prefer migration 009's original cutoff so applying
-- v1.4.63 cannot silently reclassify already collected v1.4.62 evidence.
CREATE TABLE IF NOT EXISTS v1463_lane_monitor_snapshot_meta (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    snapshot_max_event_id INTEGER NOT NULL CHECK(snapshot_max_event_id >= 0),
    snapshot_at_ms INTEGER NOT NULL CHECK(snapshot_at_ms >= 0),
    created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0)
);

INSERT OR IGNORE INTO v1463_lane_monitor_snapshot_meta (
    singleton_id,
    snapshot_max_event_id,
    snapshot_at_ms,
    created_at_ms
)
SELECT
    1,
    COALESCE(
        (SELECT MAX(snapshot_max_event_id)
         FROM v1462_lane_monitor_legacy_summary),
        (SELECT COALESCE(MAX(id), 0) FROM mainnet_run_events)
    ),
    COALESCE(
        (SELECT MAX(snapshot_at_ms)
         FROM v1462_lane_monitor_legacy_summary),
        (SELECT COALESCE(MAX(event_time_ms), 0) FROM mainnet_run_events)
    ),
    CAST(strftime('%s', 'now') AS INTEGER) * 1000;
