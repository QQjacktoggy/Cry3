from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_phase1_manifest.py"
PHASE1_DIR = ROOT / "reports" / "archive" / "phase1_20260710"
PHASE1_DB = PHASE1_DIR / "phase1_selected_runs_20260710.db"
REMOTE_SOURCE_DB_SHA256 = (
    "5b6c584352667bbfc2eda99fa699dd2430267dcfa7f9b5a42fb4bf2c71014c0a"
)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_subset_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE mainnet_runs (
            run_id TEXT PRIMARY KEY,
            signal_json TEXT,
            params_json TEXT
        );
        CREATE TABLE mainnet_run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            details_json TEXT NOT NULL
        );
        CREATE INDEX idx_mainnet_run_events_run_id
        ON mainnet_run_events(run_id);
        INSERT INTO mainnet_runs VALUES ('run-1', '{}', '{}');
        INSERT INTO mainnet_runs VALUES ('run-2', '{}', '{}');
        INSERT INTO mainnet_run_events(run_id, details_json) VALUES
            ('run-1', '{}'), ('run-1', '{}'), ('run-2', '{}');
        """
    )
    connection.commit()
    connection.close()


def metadata_args(source_db_sha256: str = REMOTE_SOURCE_DB_SHA256) -> list[str]:
    return [
        "--source-host",
        "cry3jack",
        "--source-version",
        "v1.4.55",
        "--source-service",
        "cry3.service",
        "--source-db-sha256",
        source_db_sha256,
        "--deploy-archive-sha256",
        "1" * 64,
        "--backup-archive-sha256",
        "2" * 64,
        "--extracted-at",
        "2026-07-10T12:00:00+08:00",
    ]


def run_builder(
    artifact_dir: Path,
    subset_db: Path,
    output: Path,
    *,
    source_db_sha256: str = REMOTE_SOURCE_DB_SHA256,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--artifact-dir",
            str(artifact_dir),
            "--subset-db",
            str(subset_db),
            "--output",
            str(output),
            *metadata_args(source_db_sha256),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_manifest_is_deterministic_and_excludes_its_output(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    nested = artifact_dir / "notes"
    nested.mkdir()
    (nested / "evidence.txt").write_text("phase 1 evidence\n", encoding="utf-8")
    subset_db = artifact_dir / "subset.db"
    make_subset_db(subset_db)
    output = artifact_dir / "manifest.json"
    output.write_text("stale manifest must be excluded\n", encoding="utf-8")

    first = run_builder(artifact_dir, subset_db, output)
    assert first.returncode == 0, first.stderr
    first_text = output.read_text(encoding="utf-8")
    second = run_builder(artifact_dir, subset_db, output)
    assert second.returncode == 0, second.stderr
    assert output.read_text(encoding="utf-8") == first_text

    manifest = json.loads(first_text)
    paths = [artifact["path"] for artifact in manifest["artifacts"]]
    assert paths == ["notes/evidence.txt", "subset.db"]
    assert "manifest.json" not in paths
    for artifact in manifest["artifacts"]:
        artifact_path = artifact_dir / artifact["path"]
        assert artifact["sha256"] == file_sha256(artifact_path)
        assert artifact["size_bytes"] == artifact_path.stat().st_size

    artifact_payload = json.dumps(
        manifest["artifacts"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert manifest["artifacts_sha256"] == hashlib.sha256(artifact_payload).hexdigest()
    assert manifest["subset_db"]["runs"] == 2
    assert manifest["subset_db"]["events"] == 3
    assert manifest["subset_db"]["sha256"] == file_sha256(subset_db)
    assert len(manifest["subset_db"]["schema_sha256"]) == 64
    assert manifest["redaction"]["enabled"] is True
    assert manifest["redaction"]["replacement"] == "[REDACTED]"
    assert manifest["source"]["db_sha256"] == REMOTE_SOURCE_DB_SHA256


def test_invalid_metadata_digest_is_rejected_before_output(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    subset_db = artifact_dir / "subset.db"
    make_subset_db(subset_db)
    output = tmp_path / "manifest.json"

    result = run_builder(
        artifact_dir,
        subset_db,
        output,
        source_db_sha256="not-a-sha256",
    )

    assert result.returncode != 0
    assert "64-character hexadecimal SHA256" in result.stderr
    assert not output.exists()


@pytest.mark.skipif(not PHASE1_DB.is_file(), reason="Phase 1 archive is not present")
def test_phase1_archive_manifest_counts_and_hashes(tmp_path: Path) -> None:
    output = tmp_path / "phase1_manifest.json"
    result = run_builder(PHASE1_DIR, PHASE1_DB, output)
    assert result.returncode == 0, result.stderr

    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["subset_db"]["runs"] == 27
    assert manifest["subset_db"]["events"] == 4451
    assert manifest["source"]["db_sha256"] == REMOTE_SOURCE_DB_SHA256
    assert manifest["subset_db"]["sha256"] == file_sha256(PHASE1_DB)
    assert manifest["subset_db"]["path"] == PHASE1_DB.name

    artifact_paths = {artifact["path"] for artifact in manifest["artifacts"]}
    assert {
        "phase1_review_runs_20260710.txt",
        "phase1_selected_runs_20260710.db",
        "phase1_service_tail_20260710.log",
        "phase1_vm_state_20260710.txt",
        "selected_run_ids.txt",
    } <= artifact_paths
    for artifact in manifest["artifacts"]:
        assert artifact["sha256"] == file_sha256(PHASE1_DIR / artifact["path"])
