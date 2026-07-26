from hashlib import sha256
import io
import json
from pathlib import Path
import zipfile

import pytest

from scripts.freeze_live_next_research import build_contract
from src.gridbot.strategy.live_next.aggtrade_cache import (
    AggTradeCacheError,
    load_cached_train_day,
)
from src.gridbot.strategy.live_next.contracts import canonical_sha256


def _zip(rows: list[str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(
            "ETHUSDC-aggTrades.csv",
            "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
            "transact_time,is_buyer_maker\n" + "\n".join(rows) + "\n",
        )
    return output.getvalue()


def _write_fixture(
    cache_dir: Path,
    *,
    timestamp_unit: str = "MILLISECONDS",
    id_gap: bool = False,
):
    contract = build_contract()
    day = contract["split_days"]["TRAIN"][0]
    base = 1_767_225_600_000 if timestamp_unit == "MILLISECONDS" else 1_767_225_600_000_000
    step = 1 if timestamp_unit == "MILLISECONDS" else 1_000
    second_id = 12 if id_gap else 11
    rows = [
        f"10,100.00,0.5,100,100,{base},false",
        f"{second_id},99.99,0.4,101,101,{base + step},true",
    ]
    archive = _zip(rows)
    archive_path = cache_dir / f"ETHUSDC-aggTrades-{day}.zip"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(archive)
    first_row = {
        "agg_trade_id": 10,
        "price": "100.00",
        "quantity": "0.5",
        "first_trade_id": 100,
        "last_trade_id": 100,
        "transact_time": base,
        "is_buyer_maker": False,
    }
    last_row = {
        "agg_trade_id": second_id,
        "price": "99.99",
        "quantity": "0.4",
        "first_trade_id": 101,
        "last_trade_id": 101,
        "transact_time": base + step,
        "is_buyer_maker": True,
    }
    manifest_body = {
        "status": "COMPLETE",
        "split": "TRAIN",
        "date_utc": day,
        "contract_hash": contract["contract_hash"],
        "archive_sha256": sha256(archive).hexdigest(),
        "row_count": 2,
        "agg_trade_id_gap_count": 0,
        "timestamp_unit": timestamp_unit,
        "first_row": first_row,
        "last_row": last_row,
    }
    manifest = {
        **manifest_body,
        "manifest_sha256": canonical_sha256(manifest_body),
    }
    (cache_dir / f"ETHUSDC-aggTrades-{day}.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    inventory_body = {
        "status": "PARTIAL_SMOKE",
        "split": "TRAIN",
        "contract_hash": contract["contract_hash"],
        "days": [day],
        "validation_accessed": False,
        "holdout_accessed": False,
        "orders_enabled": False,
        "live_deployment": False,
    }
    inventory = {
        **inventory_body,
        "inventory_sha256": canonical_sha256(inventory_body),
    }
    return contract, inventory, day, archive_path


@pytest.mark.parametrize("timestamp_unit", ["MILLISECONDS", "MICROSECONDS"])
def test_loads_only_verified_train_and_normalizes_time(tmp_path, timestamp_unit):
    contract, inventory, day, _ = _write_fixture(
        tmp_path, timestamp_unit=timestamp_unit
    )
    loaded = load_cached_train_day(
        contract=contract,
        inventory=inventory,
        cache_dir=tmp_path,
        day=day,
    )

    assert loaded.date_utc == day
    assert loaded.timestamp_unit == timestamp_unit
    assert [trade.agg_trade_id for trade in loaded.trades] == [10, 11]
    assert loaded.trades[1].transact_time_ms - loaded.trades[0].transact_time_ms == 1


def test_rejects_sealed_holdout_day(tmp_path):
    contract, inventory, _, _ = _write_fixture(tmp_path)
    with pytest.raises(AggTradeCacheError, match="HOLDOUT day is sealed"):
        load_cached_train_day(
            contract=contract,
            inventory=inventory,
            cache_dir=tmp_path,
            day=contract["split_days"]["HOLDOUT"][0],
        )


def test_rejects_archive_hash_tamper(tmp_path):
    contract, inventory, day, archive_path = _write_fixture(tmp_path)
    archive_path.write_bytes(archive_path.read_bytes() + b"tamper")
    with pytest.raises(AggTradeCacheError, match="SHA-256 mismatch"):
        load_cached_train_day(
            contract=contract,
            inventory=inventory,
            cache_dir=tmp_path,
            day=day,
        )


def test_rejects_aggregate_trade_id_gap_even_if_manifest_claims_none(tmp_path):
    contract, inventory, day, _ = _write_fixture(tmp_path, id_gap=True)
    with pytest.raises(AggTradeCacheError, match="ID gap"):
        load_cached_train_day(
            contract=contract,
            inventory=inventory,
            cache_dir=tmp_path,
            day=day,
        )
