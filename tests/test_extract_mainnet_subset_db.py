from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "extract_mainnet_subset_db.py"


def make_source(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE mainnet_runs (
            run_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            signal_json TEXT,
            params_json TEXT,
            armed_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            completed_at_ms INTEGER
        );
        CREATE INDEX idx_mainnet_runs_status_updated
        ON mainnet_runs(status, updated_at_ms);
        CREATE TABLE mainnet_run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            event_time_ms INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            details_json TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES mainnet_runs(run_id)
        );
        CREATE INDEX idx_mainnet_run_events_run_time
        ON mainnet_run_events(run_id, event_time_ms);
        """
    )
    rows = [
        (
            "cry3mn_file",
            "COMPLETED",
            json.dumps({"api_key": "a", "nested": [{"token": "t", "safe": 1}]}),
            json.dumps({"password": "p", "chat_id": 12, "safe": "yes"}),
            100,
            110,
            120,
        ),
        ("cry3mn_window", "COMPLETED", "{}", "{}", 200, 210, 220),
        ("cry3mn_active", "RUNNING", "{}", "{}", 1000, 1000, None),
        ("cry3mn_outside", "COMPLETED", "{}", "{}", 2000, 2010, 2020),
    ]
    connection.executemany(
        """
        INSERT INTO mainnet_runs
        (run_id,status,signal_json,params_json,armed_at_ms,updated_at_ms,completed_at_ms)
        VALUES (?,?,?,?,?,?,?)
        """,
        rows,
    )
    connection.executemany(
        """
        INSERT INTO mainnet_run_events
        (run_id,event_time_ms,event_type,details_json) VALUES (?,?,?,?)
        """,
        [
            (
                "cry3mn_file",
                115,
                "entry",
                json.dumps(
                    {
                        "api_secret": "s",
                        "nested": {"telegram_chat": "c", "secret_note": "n"},
                    }
                ),
            ),
            ("cry3mn_window", 205, "entry", "{}"),
            ("cry3mn_active", 1005, "entry", "{}"),
            ("cry3mn_outside", 2005, "entry", "{}"),
        ],
    )
    connection.commit()
    connection.close()


def run_export(source: Path, output: Path, *selectors: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--db",
            str(source),
            "--out-db",
            str(output),
            *selectors,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_run_id_file_is_deterministic_redacted_and_source_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    make_source(source)
    run_ids = tmp_path / "run_ids.txt"
    run_ids.write_text(
        "# selected evidence\ncry3mn_file\ncry3mn_file # duplicate\n",
        encoding="utf-8",
    )
    before_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    first = run_export(source, tmp_path / "first.db", "--run-id-file", str(run_ids))
    second = run_export(source, tmp_path / "second.db", "--run-id-file", str(run_ids))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout == second.stdout
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before_hash
    summary = json.loads(first.stdout)
    assert summary["run_ids"] == ["cry3mn_file"]
    assert summary["runs"] == 1
    assert summary["events"] == 1
    assert summary["redaction"]["redacted_values"] == 7

    output = sqlite3.connect(tmp_path / "first.db")
    run = output.execute(
        "SELECT signal_json,params_json FROM mainnet_runs"
    ).fetchone()
    event = output.execute(
        "SELECT details_json FROM mainnet_run_events"
    ).fetchone()
    output.close()
    signal = json.loads(run[0])
    params = json.loads(run[1])
    details = json.loads(event[0])
    assert signal == {
        "api_key": "[REDACTED]",
        "nested": [{"safe": 1, "token": "[REDACTED]"}],
    }
    assert params == {
        "chat_id": "[REDACTED]",
        "password": "[REDACTED]",
        "safe": "yes",
    }
    assert details == {
        "api_secret": "[REDACTED]",
        "nested": {
            "secret_note": "[REDACTED]",
            "telegram_chat": "[REDACTED]",
        },
    }

    original = sqlite3.connect(source)
    assert "a" in original.execute(
        "SELECT signal_json FROM mainnet_runs WHERE run_id='cry3mn_file'"
    ).fetchone()[0]
    original.close()


def test_no_redact_json_preserves_payload(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    make_source(source)
    run_ids = tmp_path / "run_ids.txt"
    run_ids.write_text("cry3mn_file\n", encoding="utf-8")
    result = run_export(
        source,
        tmp_path / "plain.db",
        "--run-id-file",
        str(run_ids),
        "--no-redact-json",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["redaction"]["enabled"] is False
    output = sqlite3.connect(tmp_path / "plain.db")
    signal = json.loads(
        output.execute("SELECT signal_json FROM mainnet_runs").fetchone()[0]
    )
    output.close()
    assert signal["api_key"] == "a"
    assert signal["nested"][0]["token"] == "t"


def test_time_window_unions_active_runs(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    make_source(source)
    result = run_export(
        source,
        tmp_path / "window.db",
        "--since-ms",
        "190",
        "--until-ms",
        "230",
        "--include-active",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["run_ids"] == [
        "cry3mn_active",
        "cry3mn_window",
    ]


@pytest.mark.parametrize(
    ("selectors", "message"),
    [
        ((), "provide --run-id-file"),
        (("--since-ms", "100"), "requires both --since and --until"),
        (("--since-ms", "200", "--until-ms", "100"), "--until must be >= --since"),
    ],
)
def test_selection_validation(
    tmp_path: Path, selectors: tuple[str, ...], message: str
) -> None:
    source = tmp_path / "source.db"
    make_source(source)
    result = run_export(source, tmp_path / "out.db", *selectors)
    assert result.returncode != 0
    assert message in result.stderr
