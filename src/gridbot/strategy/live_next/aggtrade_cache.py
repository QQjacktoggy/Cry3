"""Cache-only loader for checksum-audited Binance Futures aggTrades.

This module deliberately has no downloader, URL opener, credential, runtime,
or order dependency.  It opens only days already authorized by a frozen TRAIN
contract and its inventory.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import io
import itertools
import json
from pathlib import Path
from typing import Any, Mapping
import zipfile

from scripts.freeze_live_next_research import validate_contract

from .contracts import ContractError, canonical_sha256
from .exact_replay import ExactAggTrade


EXPECTED_COLUMNS = (
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
)
ALLOWED_INVENTORY_STATUS = frozenset({"COMPLETE", "PARTIAL_SMOKE"})


class AggTradeCacheError(ContractError):
    """Cached evidence is missing, unauthorized, or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class LoadedAggTradeDay:
    date_utc: str
    trades: tuple[ExactAggTrade, ...]
    archive_sha256: str
    manifest_sha256: str
    timestamp_unit: str
    source_first_time: int
    source_last_time: int

    def __post_init__(self) -> None:
        if not self.trades:
            raise AggTradeCacheError("loaded aggTrade day cannot be empty")
        if self.timestamp_unit not in {"MILLISECONDS", "MICROSECONDS"}:
            raise AggTradeCacheError("unsupported timestamp unit")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AggTradeCacheError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise AggTradeCacheError(f"{label} must be a JSON object")
    return value


def _self_hash(value: Mapping[str, Any], field: str) -> str:
    expected = canonical_sha256({key: item for key, item in value.items() if key != field})
    if value.get(field) != expected:
        raise AggTradeCacheError(f"{field} mismatch")
    return expected


def _validate_inventory(
    contract: Mapping[str, Any], inventory: Mapping[str, Any]
) -> tuple[str, ...]:
    _self_hash(inventory, "inventory_sha256")
    if inventory.get("status") not in ALLOWED_INVENTORY_STATUS:
        raise AggTradeCacheError("TRAIN inventory is not usable")
    if inventory.get("split") != "TRAIN":
        raise AggTradeCacheError("only TRAIN inventory is authorized")
    if inventory.get("contract_hash") != contract.get("contract_hash"):
        raise AggTradeCacheError("TRAIN inventory contract hash mismatch")
    if inventory.get("validation_accessed") is not False:
        raise AggTradeCacheError("VALIDATION access is forbidden before frontier freeze")
    if inventory.get("holdout_accessed") is not False:
        raise AggTradeCacheError("HOLDOUT access is forbidden before portfolio freeze")
    if inventory.get("orders_enabled") is not False:
        raise AggTradeCacheError("inventory must prohibit orders")
    if inventory.get("live_deployment") is not False:
        raise AggTradeCacheError("inventory must prohibit live deployment")
    days_value = inventory.get("days")
    if not isinstance(days_value, list) or not days_value:
        raise AggTradeCacheError("TRAIN inventory has no cached days")
    days = tuple(str(day) for day in days_value)
    train_days = tuple(str(day) for day in contract["split_days"]["TRAIN"])
    if len(set(days)) != len(days) or any(day not in train_days for day in days):
        raise AggTradeCacheError("inventory contains a non-TRAIN or duplicate day")
    if inventory.get("status") == "COMPLETE" and days != train_days:
        raise AggTradeCacheError("COMPLETE TRAIN inventory must contain all frozen days")
    return days


def _normalize_time(source_time: int, unit: str) -> int:
    if unit == "MILLISECONDS":
        return source_time
    if unit == "MICROSECONDS":
        return source_time // 1_000
    raise AggTradeCacheError("unsupported timestamp unit")


def _parse_archive(
    archive: bytes,
    *,
    timestamp_unit: str,
) -> tuple[tuple[ExactAggTrade, ...], dict[str, Any], dict[str, Any]]:
    try:
        bundle = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile as exc:
        raise AggTradeCacheError("cached archive is not a valid ZIP") from exc
    trades: list[ExactAggTrade] = []
    first_row: dict[str, Any] | None = None
    last_row: dict[str, Any] | None = None
    previous_id: int | None = None
    previous_source_time: int | None = None
    with bundle:
        members = [item for item in bundle.infolist() if not item.is_dir()]
        if len(members) != 1 or not members[0].filename.lower().endswith(".csv"):
            raise AggTradeCacheError("aggTrades ZIP must contain exactly one CSV")
        with bundle.open(members[0]) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
            first = next(reader, None)
            if first is None:
                raise AggTradeCacheError("aggTrades CSV is empty")
            header = tuple(value.strip().lower() for value in first)
            rows = reader if header == EXPECTED_COLUMNS else itertools.chain((first,), reader)
            for row in rows:
                if len(row) != len(EXPECTED_COLUMNS):
                    raise AggTradeCacheError("aggTrades row is not seven-column schema")
                try:
                    aggregate_id = int(row[0])
                    price = Decimal(row[1])
                    quantity = Decimal(row[2])
                    first_trade_id = int(row[3])
                    last_trade_id = int(row[4])
                    source_time = int(row[5])
                except (ValueError, InvalidOperation) as exc:
                    raise AggTradeCacheError("aggTrades row has invalid numeric data") from exc
                maker_text = row[6].strip().lower()
                if maker_text not in {"true", "false"}:
                    raise AggTradeCacheError("is_buyer_maker must be true or false")
                if (
                    aggregate_id < 0
                    or price <= 0
                    or quantity <= 0
                    or first_trade_id < 0
                    or last_trade_id < first_trade_id
                ):
                    raise AggTradeCacheError("aggTrades row violates value invariants")
                if previous_id is not None:
                    if aggregate_id != previous_id + 1:
                        raise AggTradeCacheError("aggregate trade ID gap or regression")
                    assert previous_source_time is not None
                    if source_time < previous_source_time:
                        raise AggTradeCacheError("aggTrade source time regressed")
                normalized = {
                    "agg_trade_id": aggregate_id,
                    "price": str(price),
                    "quantity": str(quantity),
                    "first_trade_id": first_trade_id,
                    "last_trade_id": last_trade_id,
                    "transact_time": source_time,
                    "is_buyer_maker": maker_text == "true",
                }
                first_row = normalized if first_row is None else first_row
                last_row = normalized
                trades.append(
                    ExactAggTrade(
                        _normalize_time(source_time, timestamp_unit),
                        aggregate_id,
                        price,
                        quantity,
                        maker_text == "true",
                    )
                )
                previous_id = aggregate_id
                previous_source_time = source_time
    if not trades or first_row is None or last_row is None:
        raise AggTradeCacheError("aggTrades archive has no data rows")
    return tuple(trades), first_row, last_row


def load_cached_train_day(
    *,
    contract: Mapping[str, Any],
    inventory: Mapping[str, Any],
    cache_dir: Path,
    day: str,
) -> LoadedAggTradeDay:
    """Load one already-downloaded, inventory-authorized TRAIN day."""

    validate_contract(dict(contract))
    authorized_days = _validate_inventory(contract, inventory)
    day = str(day)
    if day not in authorized_days:
        if day in contract["split_days"]["VALIDATION"]:
            raise AggTradeCacheError("VALIDATION day is sealed")
        if day in contract["split_days"]["HOLDOUT"]:
            raise AggTradeCacheError("HOLDOUT day is sealed")
        raise AggTradeCacheError("day is absent from the TRAIN inventory")

    manifest_path = cache_dir / f"ETHUSDC-aggTrades-{day}.manifest.json"
    archive_path = cache_dir / f"ETHUSDC-aggTrades-{day}.zip"
    manifest = _read_json(manifest_path, "day manifest")
    manifest_hash = _self_hash(manifest, "manifest_sha256")
    if manifest.get("status") != "COMPLETE" or manifest.get("split") != "TRAIN":
        raise AggTradeCacheError("day manifest is not COMPLETE TRAIN evidence")
    if manifest.get("contract_hash") != contract.get("contract_hash"):
        raise AggTradeCacheError("day manifest contract hash mismatch")
    if manifest.get("date_utc") != day:
        raise AggTradeCacheError("day manifest date mismatch")
    if manifest.get("agg_trade_id_gap_count") != 0:
        raise AggTradeCacheError("day manifest records aggregate trade ID gaps")
    timestamp_unit = str(manifest.get("timestamp_unit"))
    if timestamp_unit not in {"MILLISECONDS", "MICROSECONDS"}:
        raise AggTradeCacheError("day manifest timestamp unit is unsupported")
    try:
        archive = archive_path.read_bytes()
    except OSError as exc:
        raise AggTradeCacheError("cannot read cached aggTrades ZIP") from exc
    archive_hash = sha256(archive).hexdigest()
    if archive_hash != manifest.get("archive_sha256"):
        raise AggTradeCacheError("cached archive SHA-256 mismatch")
    trades, first_row, last_row = _parse_archive(
        archive,
        timestamp_unit=timestamp_unit,
    )
    if len(trades) != manifest.get("row_count"):
        raise AggTradeCacheError("cached archive row count mismatch")
    if first_row != manifest.get("first_row") or last_row != manifest.get("last_row"):
        raise AggTradeCacheError("cached archive boundary rows mismatch")
    return LoadedAggTradeDay(
        date_utc=day,
        trades=trades,
        archive_sha256=archive_hash,
        manifest_sha256=manifest_hash,
        timestamp_unit=timestamp_unit,
        source_first_time=int(first_row["transact_time"]),
        source_last_time=int(last_row["transact_time"]),
    )


def load_cached_train_day_from_paths(
    *,
    contract_path: Path,
    inventory_path: Path,
    cache_dir: Path,
    day: str,
) -> LoadedAggTradeDay:
    return load_cached_train_day(
        contract=_read_json(contract_path, "research contract"),
        inventory=_read_json(inventory_path, "TRAIN inventory"),
        cache_dir=cache_dir,
        day=day,
    )


__all__ = [
    "AggTradeCacheError",
    "LoadedAggTradeDay",
    "load_cached_train_day",
    "load_cached_train_day_from_paths",
]
