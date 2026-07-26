from __future__ import annotations

from pathlib import Path

import pytest

from src.gridbot.mainnet.v1459_runtime_identity_source import (
    RuntimeIdentitySourceError,
    build_observed_runtime_identity,
    canonical_config_hash,
    read_account_fingerprint_marker,
)


def test_account_marker_is_non_secret_explicit_input(tmp_path: Path) -> None:
    marker = tmp_path / "account.fingerprint"
    marker.write_text("cry3-main-account-01\n", encoding="utf-8")
    assert read_account_fingerprint_marker(marker) == "cry3-main-account-01"


@pytest.mark.parametrize("value", ["", "short", "contains space", "x" * 129])
def test_account_marker_rejects_missing_or_ambiguous_values(
    tmp_path: Path, value: str
) -> None:
    marker = tmp_path / "account.fingerprint"
    marker.write_text(value, encoding="utf-8")
    with pytest.raises(RuntimeIdentitySourceError):
        read_account_fingerprint_marker(marker)


def test_config_hash_is_order_independent_and_rejects_secret_keys() -> None:
    assert canonical_config_hash({"symbol": "ETHUSDC", "notional": 50}) == (
        canonical_config_hash({"notional": 50, "symbol": "ETHUSDC"})
    )
    with pytest.raises(RuntimeIdentitySourceError, match="secret-bearing"):
        canonical_config_hash({"mainnet_api_key": "do-not-hash"})


def test_observed_identity_uses_resolved_db_and_normalised_runtime_facts(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "account.fingerprint"
    marker.write_text("cry3-main-account-01", encoding="utf-8")
    identity = build_observed_runtime_identity(
        environment="mainnet",
        exchange_endpoint="https://fapi.binance.com/",
        exchange_testnet=False,
        account_fingerprint_marker=marker,
        db_path=tmp_path / "gridbot_mainnet.db",
        symbol="ethusdc",
        account_mode="one_way",
        deployment_commit="abcdef1234567",
        config={"symbol": "ETHUSDC", "notional_usdc": 50, "dca": False},
    )
    assert identity.exchange_endpoint == "https://fapi.binance.com"
    assert identity.account_fingerprint == "cry3-main-account-01"
    assert identity.db_namespace == str((tmp_path / "gridbot_mainnet.db").resolve())
    assert identity.symbol == "ETHUSDC"
    assert identity.account_mode == "ONE_WAY"
    assert len(identity.config_hash) == 64


def test_unknown_account_mode_or_unpinned_commit_fails_closed(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "account.fingerprint"
    marker.write_text("cry3-main-account-01", encoding="utf-8")
    base = {
        "environment": "mainnet",
        "exchange_endpoint": "https://fapi.binance.com",
        "exchange_testnet": False,
        "account_fingerprint_marker": marker,
        "db_path": tmp_path / "db.sqlite",
        "symbol": "ETHUSDC",
        "account_mode": "ONE_WAY",
        "deployment_commit": "abcdef1",
        "config": {"symbol": "ETHUSDC"},
    }
    with pytest.raises(RuntimeIdentitySourceError, match="account mode"):
        build_observed_runtime_identity(**dict(base, account_mode="UNKNOWN"))
    with pytest.raises(RuntimeIdentitySourceError, match="commit"):
        build_observed_runtime_identity(**dict(base, deployment_commit="latest"))

