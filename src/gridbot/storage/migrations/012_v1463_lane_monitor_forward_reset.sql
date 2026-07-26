-- Reset the v1.4.63 Lane Monitor boundary once, after legacy reconciliation
-- tombstones were written immediately following migration 010.  The marker
-- freezes the same reset point when this SQL is replayed, so the migration is
-- idempotent and can never advance over later forward evidence.

CREATE TABLE IF NOT EXISTS v1463_lane_monitor_forward_reset_meta (
    singleton_id                  INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    previous_snapshot_max_event_id INTEGER NOT NULL,
    reset_max_event_id            INTEGER NOT NULL,
    reset_at_ms                   INTEGER NOT NULL,
    created_at_ms                 INTEGER NOT NULL
);

INSERT OR IGNORE INTO v1463_lane_monitor_forward_reset_meta (
    singleton_id,
    previous_snapshot_max_event_id,
    reset_max_event_id,
    reset_at_ms,
    created_at_ms
)
SELECT
    1,
    COALESCE((
        SELECT snapshot_max_event_id
        FROM v1463_lane_monitor_snapshot_meta
        WHERE singleton_id = 1
    ), 0),
    COALESCE((SELECT MAX(id) FROM mainnet_run_events), 0),
    CAST(strftime('%s', 'now') AS INTEGER) * 1000,
    CAST(strftime('%s', 'now') AS INTEGER) * 1000;

UPDATE v1463_lane_monitor_snapshot_meta
SET snapshot_max_event_id = (
        SELECT reset_max_event_id
        FROM v1463_lane_monitor_forward_reset_meta
        WHERE singleton_id = 1
    ),
    snapshot_at_ms = (
        SELECT reset_at_ms
        FROM v1463_lane_monitor_forward_reset_meta
        WHERE singleton_id = 1
    ),
    created_at_ms = (
        SELECT created_at_ms
        FROM v1463_lane_monitor_forward_reset_meta
        WHERE singleton_id = 1
    )
WHERE singleton_id = 1;
