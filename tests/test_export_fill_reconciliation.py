import json
import sqlite3

import pytest

from scripts.export_fill_reconciliation import export_reconciliation


def _database(tmp_path):
    path = tmp_path / "runs.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE mainnet_runs (
            run_id TEXT PRIMARY KEY,
            symbol TEXT,
            strategy_label TEXT,
            status TEXT,
            exit_reason TEXT,
            armed_at_ms INTEGER
        );
        CREATE TABLE mainnet_run_events (
            run_id TEXT,
            event_time_ms INTEGER,
            event_type TEXT,
            details_json TEXT
        );
        """
    )
    return path, conn


def _run(conn, run_id, armed_at_ms, status="COMPLETED"):
    conn.execute(
        "INSERT INTO mainnet_runs VALUES (?, 'ETHUSDC', 'codex', ?, 'done', ?)",
        (run_id, status, armed_at_ms),
    )


def _fill(conn, run_id, event_time_ms, fill_key, role, **overrides):
    details = {
        "schema": "fill_v1",
        "run_id": run_id,
        "fill_key": fill_key,
        "trade_id": int(fill_key.split(":")[0]),
        "order_id": int(fill_key.split(":")[1]),
        "symbol": "ETHUSDC",
        "side": "BUY" if role in {"entry", "recovery_entry"} else "SELL",
        "price": 100.0,
        "qty": 0.1,
        "time_ms": event_time_ms,
        "role": role,
        "realized_pnl": 0.2 if role not in {"entry", "recovery_entry"} else 0.0,
        "commission": 0.01,
    }
    details.update(overrides)
    conn.execute(
        "INSERT INTO mainnet_run_events VALUES (?, ?, 'fill_v1', ?)",
        (run_id, event_time_ms, json.dumps(details)),
    )


def test_explicit_cutoff_classifies_all_reconciliation_states(tmp_path):
    path, conn = _database(tmp_path)
    _run(conn, "pre", 999)
    _run(conn, "complete", 1000)
    _run(conn, "partial", 1001, "RUNNING")
    _run(conn, "missing", 1002)
    _run(conn, "ambiguous", 1003)
    _fill(conn, "complete", 1100, "1:11", "entry")
    _fill(conn, "complete", 1200, "2:12", "final_exit")
    _fill(conn, "partial", 1300, "3:13", "entry")
    _fill(conn, "ambiguous", 1400, "4:14", "entry")
    _fill(conn, "ambiguous", 1401, "4:14", "entry")
    conn.commit()
    conn.close()

    payload = export_reconciliation(path, schema_deployment_cutoff_ms=1000)
    statuses = {run["run_id"]: run["fill_evidence_status"] for run in payload["runs"]}
    assert statuses == {
        "pre": "PRE_SCHEMA",
        "complete": "OBSERVED_COMPLETE",
        "partial": "OBSERVED_PARTIAL",
        "missing": "MISSING_EXPECTED",
        "ambiguous": "AMBIGUOUS",
    }
    assert payload["schema_deployment_cutoff_source"] == "explicit"
    assert payload["summary"]["status_counts"] == {
        "PRE_SCHEMA": 1,
        "OBSERVED_COMPLETE": 1,
        "OBSERVED_PARTIAL": 1,
        "MISSING_EXPECTED": 1,
        "AMBIGUOUS": 1,
    }
    assert payload["summary"]["anomaly_counts"] == {
        "duplicate_fill_key": 1,
        "terminal_run_without_fills": 1,
    }


def test_detects_unknown_role_and_invalid_events(tmp_path):
    path, conn = _database(tmp_path)
    _run(conn, "unknown", 1000)
    _run(conn, "invalid", 1001)
    _fill(conn, "unknown", 1100, "1:11", "unknown_exchange_fill")
    conn.execute(
        "INSERT INTO mainnet_run_events VALUES (?, ?, 'fill_v1', ?)",
        ("invalid", 1200, "{not-json"),
    )
    conn.commit()
    conn.close()

    payload = export_reconciliation(path, schema_deployment_cutoff_ms=1000)
    runs = {run["run_id"]: run for run in payload["runs"]}
    assert runs["unknown"]["fill_evidence_status"] == "AMBIGUOUS"
    assert runs["unknown"]["anomalies"] == ["unknown_role"]
    assert runs["invalid"]["fill_evidence_status"] == "AMBIGUOUS"
    assert runs["invalid"]["anomalies"] == ["invalid_event"]
    assert payload["invalid_fill_event_count"] == 1
    assert payload["summary"]["anomaly_counts"] == {
        "invalid_event": 1,
        "unknown_role": 1,
    }


def test_infers_cutoff_from_first_fill_event(tmp_path):
    path, conn = _database(tmp_path)
    _run(conn, "pre", 1499)
    _run(conn, "observed", 1500)
    _fill(conn, "observed", 1500, "1:11", "entry")
    conn.commit()
    conn.close()

    payload = export_reconciliation(path)
    assert payload["schema_deployment_cutoff_ms"] == 1500
    assert payload["schema_deployment_cutoff_source"] == "first_fill_v1_event"
    assert [run["fill_evidence_status"] for run in payload["runs"]] == [
        "PRE_SCHEMA", "OBSERVED_PARTIAL",
    ]


def test_requires_explicit_cutoff_when_database_has_no_fill_events(tmp_path):
    path, conn = _database(tmp_path)
    _run(conn, "run", 1000)
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="cannot infer schema deployment cutoff"):
        export_reconciliation(path)
