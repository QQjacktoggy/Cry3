from __future__ import annotations

from types import SimpleNamespace

from src.gridbot.mainnet.runtime_identity import RuntimeIdentity
from src.gridbot.mainnet.v1459_observation_bootstrap import (
    build_v1459_observation_bootstrap,
)
from src.gridbot.storage.database import Database


def _identity(**updates) -> RuntimeIdentity:
    values = {
        "environment": "mainnet",
        "exchange_endpoint": "https://fapi.binance.com",
        "exchange_testnet": False,
        "account_fingerprint": "cry3-main-account-01",
        "db_namespace": "gridbot_mainnet.db",
        "symbol": "ETHUSDC",
        "account_mode": "ONE_WAY",
        "deployment_commit": "abcdef1",
        "config_hash": "config-a",
    }
    values.update(updates)
    return RuntimeIdentity(**values)


def _settings():
    return SimpleNamespace(
        mainnet_v1459_observation_enabled=False,
        mainnet_v1459_observation_persist_session_enabled=False,
        mainnet_v1459_observation_record_opportunities_enabled=False,
        mainnet_v1459_observation_record_shadow_enabled=False,
        mainnet_v1459_observation_record_reconciliation_enabled=False,
    )


def test_bootstrap_has_no_order_capability_and_defaults_expected_to_observed(
    tmp_path,
) -> None:
    observed = _identity(db_namespace=str(tmp_path / "db.sqlite"))
    bootstrap = build_v1459_observation_bootstrap(
        settings=_settings(),
        db=Database(str(tmp_path / "db.sqlite")),
        observed_identity=observed,
        code_version="v1.4.59",
    )
    assert bootstrap.permits_order_mutation is False
    assert bootstrap.runtime.permits_order_mutation is False
    assert bootstrap.runtime.flags.enabled is False
    for candidate in (bootstrap, bootstrap.runtime, bootstrap.composition.coordinator):
        assert not hasattr(candidate, "create_order")
        assert not hasattr(candidate, "cancel_order")


def test_restart_can_supply_durable_expected_identity(tmp_path) -> None:
    expected = _identity(config_hash="old")
    observed = _identity(config_hash="new")
    bootstrap = build_v1459_observation_bootstrap(
        settings=_settings(),
        db=Database(str(tmp_path / "db.sqlite")),
        expected_identity=expected,
        observed_identity=observed,
        code_version="v1.4.59",
    )
    context = bootstrap.runtime._context
    assert context.expected_identity.config_hash == "old"
    assert context.observed_identity.config_hash == "new"

