#!/usr/bin/env python3
"""Build a deterministic manifest for the Phase 1 evidence archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Sequence

MANIFEST_VERSION = 1
READ_CHUNK_SIZE = 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
REDACTION_DECLARATION = {
    "enabled": True,
    "json_columns": [
        "mainnet_run_events.details_json",
        "mainnet_runs.params_json",
        "mainnet_runs.signal_json",
    ],
    "key_fragments": [
        "api_key",
        "apikey",
        "api_secret",
        "secret",
        "token",
        "password",
        "chat_id",
        "telegram_chat",
    ],
    "method": "recursive_json_key_fragment_match",
    "replacement": "[REDACTED]",
}


class ManifestError(ValueError):
    """Raised when the evidence bundle cannot produce a valid manifest."""


def sha256_argument(value: str) -> str:
    digest = value.strip().lower()
    if not SHA256_PATTERN.fullmatch(digest):
        raise argparse.ArgumentTypeError("expected a 64-character hexadecimal SHA256")
    return digest


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(READ_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def artifact_records(artifact_dir: Path, output: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for candidate in artifact_dir.rglob("*"):
        if candidate.is_symlink():
            raise ManifestError(f"artifact symlinks are not supported: {candidate}")
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved == output:
            continue
        records.append(
            {
                "path": candidate.relative_to(artifact_dir).as_posix(),
                "sha256": sha256_file(resolved),
                "size_bytes": resolved.stat().st_size,
            }
        )
    records.sort(key=lambda record: record["path"])
    if not records:
        raise ManifestError(f"artifact directory contains no files: {artifact_dir}")
    return records


def portable_subset_path(subset_db: Path, artifact_dir: Path) -> str:
    try:
        return subset_db.relative_to(artifact_dir).as_posix()
    except ValueError:
        return subset_db.as_posix()


def sqlite_summary(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        runs = int(connection.execute("SELECT COUNT(*) FROM mainnet_runs").fetchone()[0])
        events = int(
            connection.execute("SELECT COUNT(*) FROM mainnet_run_events").fetchone()[0]
        )
        schema_rows = [
            {
                "name": row[1],
                "sql": row[3],
                "table": row[2],
                "type": row[0],
            }
            for row in connection.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_schema
                WHERE sql IS NOT NULL
                ORDER BY type, name, tbl_name, sql
                """
            )
        ]
    except sqlite3.Error as exc:
        raise ManifestError(f"unable to inspect subset DB {path}: {exc}") from exc
    finally:
        connection.close()

    if not schema_rows:
        raise ManifestError(f"subset DB has no SQLite schema: {path}")
    return {
        "events": events,
        "runs": runs,
        "schema_sha256": hashlib.sha256(canonical_json_bytes(schema_rows)).hexdigest(),
        "sha256": sha256_file(path),
    }


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    artifact_dir = Path(args.artifact_dir).resolve()
    subset_db = Path(args.subset_db).resolve()
    output = Path(args.output).resolve()

    if not artifact_dir.is_dir():
        raise ManifestError(f"artifact directory does not exist: {artifact_dir}")
    if not subset_db.is_file():
        raise ManifestError(f"subset DB does not exist: {subset_db}")
    if output == subset_db:
        raise ManifestError("--output must not overwrite --subset-db")

    artifacts = artifact_records(artifact_dir, output)
    subset = sqlite_summary(subset_db)
    subset["path"] = portable_subset_path(subset_db, artifact_dir)

    return {
        "artifacts": artifacts,
        "artifacts_sha256": hashlib.sha256(canonical_json_bytes(artifacts)).hexdigest(),
        "extracted_at": args.extracted_at,
        "manifest_version": MANIFEST_VERSION,
        "redaction": dict(REDACTION_DECLARATION),
        "source": {
            "backup_archive_sha256": args.backup_archive_sha256,
            "db_sha256": args.source_db_sha256,
            "deploy_archive_sha256": args.deploy_archive_sha256,
            "host": args.source_host,
            "service": args.source_service,
            "version": args.source_version,
        },
        "subset_db": subset,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the deterministic Phase 1 evidence manifest."
    )
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--subset-db", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-host", required=True)
    parser.add_argument("--source-version", required=True)
    parser.add_argument("--source-service", required=True)
    parser.add_argument("--source-db-sha256", required=True, type=sha256_argument)
    parser.add_argument("--deploy-archive-sha256", required=True, type=sha256_argument)
    parser.add_argument("--backup-archive-sha256", required=True, type=sha256_argument)
    parser.add_argument("--extracted-at", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        manifest = build_manifest(args)
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (ManifestError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
