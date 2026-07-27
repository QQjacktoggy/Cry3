#!/usr/bin/env python3
"""Extract selected mainnet runs into a redacted SQLite subset."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

TPE = dt.timezone(dt.timedelta(hours=8))
ACTIVE_STATUSES = ("ARMED", "ENTRY_PENDING", "RUNNING", "CLOSING")
SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "api_secret",
    "secret",
    "token",
    "password",
    "chat_id",
    "telegram_chat",
)
REDACTION_REPLACEMENT = "[REDACTED]"
JSON_COLUMNS = {
    "mainnet_runs": ("signal_json", "params_json"),
    "mainnet_run_events": ("details_json",),
}


def parse_tpe(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid TPE timestamp {value!r}; expected YYYY-MM-DD HH:MM:SS"
        ) from exc
    return int(parsed.replace(tzinfo=TPE).timestamp() * 1000)


def connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SystemExit(f"Source DB does not exist: {path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    connection.row_factory = sqlite3.Row
    return connection


def load_run_ids(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise SystemExit(f"Unable to read --run-id-file {path}: {exc}") from exc

    run_ids: set[str] = set()
    for line_number, raw_line in enumerate(lines, start=1):
        run_id = raw_line.split("#", 1)[0].strip()
        if not run_id:
            continue
        if any(character.isspace() for character in run_id):
            raise SystemExit(
                f"Invalid run ID at {path}:{line_number}; expected one run ID per line"
            )
        run_ids.add(run_id)
    if not run_ids:
        raise SystemExit(f"--run-id-file contains no run IDs: {path}")
    return sorted(run_ids)


def copy_schema(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> None:
    row = src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not row or not row[0]:
        raise SystemExit(f"Missing table schema: {table}")
    dst.execute(str(row[0]))


def copy_indexes(src: sqlite3.Connection, dst: sqlite3.Connection) -> None:
    for row in src.execute(
        """
        SELECT sql FROM sqlite_master
        WHERE type='index' AND sql IS NOT NULL
          AND tbl_name IN ('mainnet_runs', 'mainnet_run_events')
        ORDER BY name
        """
    ):
        dst.execute(str(row[0]))


def redact_value(value: Any) -> tuple[Any, int]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        count = 0
        for key, child in value.items():
            folded = str(key).casefold()
            if any(fragment in folded for fragment in SENSITIVE_KEY_FRAGMENTS):
                result[key] = REDACTION_REPLACEMENT
                count += 1
            else:
                result[key], child_count = redact_value(child)
                count += child_count
        return result, count
    if isinstance(value, list):
        result_list: list[Any] = []
        count = 0
        for child in value:
            redacted, child_count = redact_value(child)
            result_list.append(redacted)
            count += child_count
        return result_list, count
    return value, 0


def redact_rows(
    table: str, rows: list[sqlite3.Row], enabled: bool
) -> tuple[list[dict[str, Any]], int]:
    copied_rows: list[dict[str, Any]] = []
    total = 0
    for row in rows:
        copied = dict(row)
        if enabled:
            identity = copied.get("run_id", copied.get("id", "unknown"))
            for column in JSON_COLUMNS[table]:
                raw = copied.get(column)
                if raw is None:
                    continue
                try:
                    parsed = json.loads(str(raw))
                except json.JSONDecodeError as exc:
                    raise SystemExit(
                        f"Invalid JSON in {table}.{column} row={identity}; "
                        "refusing an unredacted copy"
                    ) from exc
                redacted, count = redact_value(parsed)
                copied[column] = json.dumps(
                    redacted, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                total += count
        copied_rows.append(copied)
    return copied_rows, total


def existing_run_ids(src: sqlite3.Connection, run_ids: list[str]) -> set[str]:
    existing: set[str] = set()
    for offset in range(0, len(run_ids), 900):
        chunk = run_ids[offset : offset + 900]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        existing.update(
            str(row["run_id"])
            for row in src.execute(
                f"SELECT run_id FROM mainnet_runs WHERE run_id IN ({placeholders})",
                chunk,
            )
        )
    return existing


def select_run_ids(
    src: sqlite3.Connection,
    explicit: list[str],
    since_ms: int | None,
    until_ms: int | None,
    include_active: bool,
) -> list[str]:
    selected: set[str] = set()
    if explicit:
        found = existing_run_ids(src, explicit)
        missing = sorted(set(explicit) - found)
        if missing:
            raise SystemExit(f"Run IDs not found in source DB: {', '.join(missing)}")
        selected.update(found)

    if since_ms is not None and until_ms is not None:
        selected.update(
            str(row["run_id"])
            for row in src.execute(
                """
                SELECT DISTINCT run_id FROM mainnet_run_events
                WHERE event_time_ms BETWEEN ? AND ?
                """,
                (since_ms, until_ms),
            )
        )
        selected.update(
            str(row["run_id"])
            for row in src.execute(
                """
                SELECT run_id FROM mainnet_runs
                WHERE armed_at_ms BETWEEN ? AND ?
                   OR updated_at_ms BETWEEN ? AND ?
                   OR completed_at_ms BETWEEN ? AND ?
                """,
                (since_ms, until_ms, since_ms, until_ms, since_ms, until_ms),
            )
        )

    if include_active:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        selected.update(
            str(row["run_id"])
            for row in src.execute(
                f"SELECT run_id FROM mainnet_runs WHERE status IN ({placeholders})",
                ACTIVE_STATUSES,
            )
        )

    selected = existing_run_ids(src, sorted(selected))
    if not selected:
        raise SystemExit("No runs selected")
    return sorted(selected)


def fetch_rows(
    src: sqlite3.Connection, run_ids: list[str]
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    runs: list[sqlite3.Row] = []
    events: list[sqlite3.Row] = []
    for offset in range(0, len(run_ids), 900):
        chunk = run_ids[offset : offset + 900]
        placeholders = ",".join("?" for _ in chunk)
        runs.extend(
            src.execute(
                f"SELECT * FROM mainnet_runs WHERE run_id IN ({placeholders})", chunk
            )
        )
        events.extend(
            src.execute(
                f"SELECT * FROM mainnet_run_events WHERE run_id IN ({placeholders})",
                chunk,
            )
        )
    runs.sort(key=lambda row: str(row["run_id"]))
    events.sort(key=lambda row: (int(row["id"]), str(row["run_id"])))
    return runs, events


def insert_rows(
    dst: sqlite3.Connection, table: str, rows: list[dict[str, Any]]
) -> None:
    if not rows:
        return
    columns = list(rows[0])
    quoted = ",".join(f'"{column}"' for column in columns)
    placeholders = ",".join("?" for _ in columns)
    dst.executemany(
        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
        [tuple(row[column] for column in columns) for row in rows],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract mainnet_runs/mainnet_run_events into a small SQLite subset."
    )
    parser.add_argument("--db", required=True)
    parser.add_argument("--out-db", required=True)
    parser.add_argument("--run-id-file")
    since = parser.add_mutually_exclusive_group()
    since.add_argument("--since-ms", type=int)
    since.add_argument("--since-tpe")
    until = parser.add_mutually_exclusive_group()
    until.add_argument("--until-ms", type=int)
    until.add_argument("--until-tpe")
    parser.add_argument("--include-active", action="store_true")
    parser.add_argument("--no-redact-json", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        since_ms = args.since_ms if args.since_ms is not None else parse_tpe(args.since_tpe)
        until_ms = args.until_ms if args.until_ms is not None else parse_tpe(args.until_tpe)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    if (since_ms is None) != (until_ms is None):
        parser.error("a time window requires both --since and --until")
    if since_ms is not None and until_ms is not None and until_ms < since_ms:
        parser.error("--until must be >= --since")
    if not args.run_id_file and since_ms is None and not args.include_active:
        parser.error("provide --run-id-file, a complete time window, or --include-active")

    explicit = load_run_ids(Path(args.run_id_file)) if args.run_id_file else []
    source_path = Path(args.db).resolve()
    out_path = Path(args.out_db).resolve()
    if source_path == out_path:
        parser.error("--out-db must not be the source --db")

    src = connect_read_only(source_path)
    try:
        run_ids = select_run_ids(src, explicit, since_ms, until_ms, args.include_active)
        raw_runs, raw_events = fetch_rows(src, run_ids)
        runs, run_redactions = redact_rows(
            "mainnet_runs", raw_runs, not args.no_redact_json
        )
        events, event_redactions = redact_rows(
            "mainnet_run_events", raw_events, not args.no_redact_json
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        if out_path.exists():
            out_path.unlink()
        dst = sqlite3.connect(out_path)
        try:
            dst.execute("PRAGMA foreign_keys = ON")
            copy_schema(src, dst, "mainnet_runs")
            copy_schema(src, dst, "mainnet_run_events")
            insert_rows(dst, "mainnet_runs", runs)
            insert_rows(dst, "mainnet_run_events", events)
            copy_indexes(src, dst)
            dst.commit()
        except BaseException:
            dst.close()
            out_path.unlink(missing_ok=True)
            raise
        else:
            dst.close()
    finally:
        src.close()

    summary = {
        "events": len(events),
        "redaction": {
            "enabled": not args.no_redact_json,
            "json_columns": [
                "mainnet_run_events.details_json",
                "mainnet_runs.params_json",
                "mainnet_runs.signal_json",
            ],
            "key_fragments": list(SENSITIVE_KEY_FRAGMENTS),
            "redacted_values": run_redactions + event_redactions,
            "replacement": REDACTION_REPLACEMENT,
        },
        "run_ids": run_ids,
        "runs": len(runs),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
