#!/usr/bin/env python3
"""Validate the repository's line-delimited strategy evidence index."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = (
    "type", "id", "date", "topic", "summary", "source_file", "source_symbol", "status",
)
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = REPO_ROOT / "reports" / "strategy_evidence_index.jsonl"


def _source_path(repo_root: Path, source_file: str) -> Path | None:
    relative = Path(source_file)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    candidate = (repo_root / relative).resolve()
    try:
        candidate.relative_to(repo_root.resolve())
    except ValueError:
        return None
    return candidate


def validate_index(index_path: Path, repo_root: Path = REPO_ROOT) -> list[str]:
    errors: list[str] = []
    try:
        lines = index_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return [f"{index_path}: cannot read index: {exc}"]

    seen_ids: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        prefix = f"{index_path}:{line_number}"
        if not line.strip():
            errors.append(f"{prefix}: empty JSONL line")
            continue
        try:
            record: Any = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{prefix}: invalid JSON: {exc.msg}")
            continue
        if not isinstance(record, dict):
            errors.append(f"{prefix}: record must be a JSON object")
            continue
        missing = [field for field in REQUIRED_FIELDS if field not in record]
        if missing:
            errors.append(f"{prefix}: missing required fields: {', '.join(missing)}")
            continue
        empty = [field for field in REQUIRED_FIELDS if not isinstance(record[field], str) or not record[field].strip()]
        if empty:
            errors.append(f"{prefix}: required fields must be non-empty strings: {', '.join(empty)}")
            continue
        record_id = record["id"]
        if record_id in seen_ids:
            errors.append(f"{prefix}: duplicate id: {record_id}")
        seen_ids.add(record_id)
        source_file = record["source_file"]
        source_symbol = record["source_symbol"]
        source_path = _source_path(repo_root, source_file)
        if source_path is None:
            errors.append(f"{prefix}: source_file must stay within repository: {source_file}")
            continue
        if not source_path.is_file():
            errors.append(f"{prefix}: source_file does not exist: {source_file}")
            continue
        try:
            source_text = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"{prefix}: cannot read source_file {source_file}: {exc}")
            continue
        if source_symbol not in source_text:
            errors.append(f"{prefix}: source_symbol not found in {source_file}: {source_symbol}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", nargs="?", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args()
    errors = validate_index(args.index, args.repo_root)
    if errors:
        for error in errors:
            print(error)
        return 1
    record_count = len(args.index.read_text(encoding="utf-8").splitlines())
    print(f"Validated {record_count} evidence records: {args.index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
