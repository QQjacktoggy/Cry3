"""Export run-level fill_v1 evidence for exchange reconciliation."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


ACTIVE_STATUSES = {"ARMED", "ENTRY_PENDING", "RUNNING", "CLOSING"}
ENTRY_ROLES = {"entry", "recovery_entry"}
EXIT_ROLES = {"partial_exit", "mid_exit", "final_exit", "stop_loss_exit", "exit"}
KNOWN_ROLES = ENTRY_ROLES | EXIT_ROLES
UNKNOWN_ROLES = {"", "unknown", "unknown_exchange_fill"}
REQUIRED_FILL_FIELDS = {
    "schema", "run_id", "fill_key", "trade_id", "order_id", "symbol",
    "side", "price", "qty", "time_ms", "role",
}


def _parse_fill(row: sqlite3.Row) -> tuple[dict[str, Any] | None, str | None]:
    try:
        details = json.loads(row["details_json"] or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "invalid_event"
    if not isinstance(details, dict) or not REQUIRED_FILL_FIELDS.issubset(details):
        return None, "invalid_event"
    if details.get("schema") != "fill_v1" or str(details.get("run_id")) != str(row["run_id"]):
        return None, "invalid_event"
    try:
        if not str(details["fill_key"]).strip():
            raise ValueError
        if int(details["trade_id"]) < 0 or int(details["order_id"]) < 0:
            raise ValueError
        if int(details["time_ms"]) <= 0 or float(details["price"]) <= 0 or float(details["qty"]) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return None, "invalid_event"
    role = str(details.get("role") or "")
    if role in UNKNOWN_ROLES or role not in KNOWN_ROLES:
        return details, "unknown_role"
    return details, None


def _derive_cutoff(events: list[sqlite3.Row], explicit_cutoff_ms: int | None) -> tuple[int, str]:
    if explicit_cutoff_ms is not None:
        if explicit_cutoff_ms < 0:
            raise ValueError("schema_deployment_cutoff_ms must be non-negative")
        return explicit_cutoff_ms, "explicit"
    if not events:
        raise ValueError(
            "cannot infer schema deployment cutoff without fill_v1 events; "
            "pass schema_deployment_cutoff_ms"
        )
    return min(int(row["event_time_ms"]) for row in events), "first_fill_v1_event"


def export_reconciliation(
    db_path: Path, schema_deployment_cutoff_ms: int | None = None
) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        runs = conn.execute(
            "SELECT * FROM mainnet_runs ORDER BY armed_at_ms ASC, run_id ASC"
        ).fetchall()
        events = conn.execute(
            "SELECT run_id, event_time_ms, details_json FROM mainnet_run_events "
            "WHERE event_type = 'fill_v1' ORDER BY event_time_ms ASC, rowid ASC"
        ).fetchall()
    finally:
        conn.close()

    cutoff_ms, cutoff_source = _derive_cutoff(events, schema_deployment_cutoff_ms)
    fills_by_run: dict[str, list[dict[str, Any]]] = {}
    anomalies_by_run: dict[str, list[str]] = {}
    seen_fill_keys: dict[str, set[str]] = {}
    anomaly_counts: Counter[str] = Counter()

    for row in events:
        run_id = str(row["run_id"])
        details, anomaly = _parse_fill(row)
        if anomaly:
            anomalies_by_run.setdefault(run_id, []).append(anomaly)
            anomaly_counts[anomaly] += 1
        if details is None:
            continue
        fill_key = str(details["fill_key"])
        seen = seen_fill_keys.setdefault(run_id, set())
        if fill_key in seen:
            anomalies_by_run.setdefault(run_id, []).append("duplicate_fill_key")
            anomaly_counts["duplicate_fill_key"] += 1
            continue
        seen.add(fill_key)
        fills_by_run.setdefault(run_id, []).append(
            {"event_time_ms": int(row["event_time_ms"]), **details}
        )

    records: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    for run in runs:
        run_id = str(run["run_id"])
        armed_at_ms = int(run["armed_at_ms"] or 0)
        fills = fills_by_run.get(run_id, [])
        anomalies = sorted(set(anomalies_by_run.get(run_id, [])))
        roles = {str(fill.get("role") or "") for fill in fills}
        terminal = str(run["status"]) not in ACTIVE_STATUSES

        if armed_at_ms < cutoff_ms:
            evidence_status = "PRE_SCHEMA"
        elif anomalies:
            evidence_status = "AMBIGUOUS"
        elif terminal and not fills:
            evidence_status = "MISSING_EXPECTED"
            anomalies = ["terminal_run_without_fills"]
            anomaly_counts["terminal_run_without_fills"] += 1
        elif terminal and roles.intersection(ENTRY_ROLES) and roles.intersection(EXIT_ROLES):
            evidence_status = "OBSERVED_COMPLETE"
        else:
            evidence_status = "OBSERVED_PARTIAL"

        status_counts[evidence_status] += 1
        realized = sum(float(fill.get("realized_pnl") or 0.0) for fill in fills)
        commission = sum(float(fill.get("commission") or 0.0) for fill in fills)
        records.append({
            "run_id": run_id,
            "symbol": run["symbol"],
            "strategy_label": run["strategy_label"],
            "status": run["status"],
            "exit_reason": run["exit_reason"],
            "armed_at_ms": armed_at_ms,
            "fill_count": len(fills),
            "fill_evidence_status": evidence_status,
            "anomalies": anomalies,
            "realized_pnl": realized,
            "commission": commission,
            "net_pnl": realized - commission,
            "fills": fills,
        })

    all_statuses = (
        "PRE_SCHEMA", "OBSERVED_COMPLETE", "OBSERVED_PARTIAL",
        "MISSING_EXPECTED", "AMBIGUOUS",
    )
    return {
        "schema": "fill_reconciliation_v2",
        "source_db": str(db_path),
        "schema_deployment_cutoff_ms": cutoff_ms,
        "schema_deployment_cutoff_source": cutoff_source,
        "run_count": len(records),
        "fill_event_count": len(events),
        "valid_unique_fill_count": sum(len(fills) for fills in fills_by_run.values()),
        "invalid_fill_event_count": anomaly_counts["invalid_event"],
        "summary": {
            "status_counts": {status: status_counts[status] for status in all_statuses},
            "anomaly_counts": dict(sorted(anomaly_counts.items())),
        },
        "runs": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--schema-deployment-cutoff-ms",
        type=int,
        help="First armed_at_ms expected to emit fill_v1; inferred from first fill_v1 event if omitted.",
    )
    args = parser.parse_args()
    payload = export_reconciliation(args.db, args.schema_deployment_cutoff_ms)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "run_count": payload["run_count"],
        "fill_event_count": payload["fill_event_count"],
        "invalid_fill_event_count": payload["invalid_fill_event_count"],
        **payload["summary"],
    }))


if __name__ == "__main__":
    main()
