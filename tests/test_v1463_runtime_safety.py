from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config.settings import Settings
from src.gridbot.core.app import App


def _settings(**overrides) -> Settings:
    values = {
        "binance_api_key": "testnet-key",
        "binance_api_secret": "testnet-secret",
    }
    values.update(overrides)
    return Settings.model_construct(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"mainnet_codex_v1462_strict_live_allowlist_enabled": True},
        {"mainnet_codex_v1462_shadow_all_enabled": True},
        {
            "mainnet_codex_v1462_strict_live_allowlist_enabled": True,
            "mainnet_codex_v1462_shadow_all_enabled": True,
            "mainnet_codex_v1462_promotion_enforcement_enabled": True,
        },
    ],
)
def test_paid_mainnet_codex_requires_closed_v1463_flag_tuple(overrides):
    settings = _settings(
        mainnet_one_run_enabled=True,
        mainnet_codex_v1_enabled=True,
        **overrides,
    )

    with pytest.raises(RuntimeError, match="unsafe v1.4.64 mainnet Codex configuration"):
        settings.assert_mainnet_v1463_runtime_safety()


def test_paid_mainnet_codex_accepts_only_strict_shadow_on_promotion_off():
    settings = _settings(
        mainnet_one_run_enabled=True,
        mainnet_strategy_label="_codex_v1.4.63",
        mainnet_codex_v1462_strict_live_allowlist_enabled=True,
        mainnet_codex_v1462_shadow_all_enabled=True,
        mainnet_codex_v1462_promotion_enforcement_enabled=False,
    )

    settings.assert_mainnet_v1463_runtime_safety()


def test_v1464_auto_promotion_accepts_closed_execution_controls():
    settings = _settings(
        mainnet_one_run_enabled=True,
        mainnet_strategy_label="_codex_v1.4.64",
        mainnet_codex_v1462_strict_live_allowlist_enabled=True,
        mainnet_codex_v1462_shadow_all_enabled=True,
        mainnet_codex_v1462_promotion_enforcement_enabled=False,
        mainnet_codex_v1464_auto_promotion_enabled=True,
        mainnet_codex_v1464_activation_cutoff_ms=1,
    )

    settings.assert_mainnet_v1463_runtime_safety()


def test_v1465_w6a_enforcement_accepts_closed_profile_contract():
    settings = _settings(
        mainnet_one_run_enabled=True,
        mainnet_strategy_label="_codex_v1.4.65",
        mainnet_codex_v1462_strict_live_allowlist_enabled=True,
        mainnet_codex_v1462_shadow_all_enabled=True,
        mainnet_codex_v1462_promotion_enforcement_enabled=False,
        mainnet_codex_v1464_auto_promotion_enabled=True,
        mainnet_codex_v1464_activation_cutoff_ms=1,
        mainnet_codex_v1465_w6a_profile_shadow_enabled=True,
        mainnet_codex_v1465_w6a_profile_selector_enabled=True,
        mainnet_codex_v1465_w6a_profile_enforcement_enabled=True,
        mainnet_codex_v1465_w6a_profile_lease_ttl_seconds=600,
        mainnet_codex_v1465_w6a_profile_notional_cap_usdc=25.0,
    )

    settings.assert_mainnet_v1463_runtime_safety()


def test_v1469_shadow_and_read_only_arbiter_accept_closed_contract():
    settings = _settings(
        mainnet_one_run_enabled=True,
        mainnet_strategy_label="_codex_v1.4.69",
        mainnet_codex_v1462_strict_live_allowlist_enabled=True,
        mainnet_codex_v1462_shadow_all_enabled=True,
        mainnet_codex_v1462_promotion_enforcement_enabled=False,
        mainnet_codex_v1464_auto_promotion_enabled=False,
        mainnet_codex_v1465_w6a_profile_enforcement_enabled=False,
        mainnet_codex_v1469_observation_enabled=True,
        mainnet_codex_v1469_paired_shadow_enabled=True,
        mainnet_codex_v1469_arbiter_enabled=True,
        mainnet_codex_v1469_live_enforcement_enabled=False,
    )

    settings.assert_mainnet_v1463_runtime_safety()


@pytest.mark.parametrize("bucket", [29, 31, 120, "invalid"])
def test_v1469_observation_requires_exact_thirty_second_bucket(bucket):
    settings = _settings(
        mainnet_one_run_enabled=True,
        mainnet_codex_v1_enabled=True,
        mainnet_codex_v1462_strict_live_allowlist_enabled=True,
        mainnet_codex_v1462_shadow_all_enabled=True,
        mainnet_codex_v1462_promotion_enforcement_enabled=False,
        mainnet_codex_v1469_observation_enabled=True,
        mainnet_codex_v1469_observation_bucket_seconds=bucket,
    )

    with pytest.raises(RuntimeError, match="v1469_observation_bucket_seconds=30"):
        settings.assert_mainnet_v1463_runtime_safety()


def test_v1469_enforcement_rejected_until_paid_claim_adapter_exists():
    settings = _settings(
        mainnet_one_run_enabled=True,
        mainnet_strategy_label="_codex_v1.4.69",
        mainnet_codex_v1462_strict_live_allowlist_enabled=True,
        mainnet_codex_v1462_shadow_all_enabled=True,
        mainnet_codex_v1462_promotion_enforcement_enabled=False,
        mainnet_codex_v1464_auto_promotion_enabled=False,
        mainnet_codex_v1465_w6a_profile_enforcement_enabled=False,
        mainnet_codex_v1469_observation_enabled=True,
        mainnet_codex_v1469_paired_shadow_enabled=True,
        mainnet_codex_v1469_arbiter_enabled=True,
        mainnet_codex_v1469_live_enforcement_enabled=True,
    )

    with pytest.raises(
        RuntimeError,
        match="paid claim adapter is available",
    ):
        settings.assert_mainnet_v1463_runtime_safety()


@pytest.mark.parametrize(
    "overrides",
    [
        {"mainnet_codex_v1469_observation_enabled": False},
        {"mainnet_codex_v1469_paired_shadow_enabled": False},
        {"mainnet_codex_v1469_arbiter_enabled": False},
        {"mainnet_codex_v1464_auto_promotion_enabled": True},
        {"mainnet_codex_v1469_safety_window_seconds": 2_700},
        {"mainnet_codex_v1469_authority_window_seconds": 10_800},
        {"mainnet_codex_v1469_submit_max_age_seconds": 61},
        {"mainnet_codex_v1469_probation_notional_usdc": 25.01},
        {"mainnet_codex_v1469_live_notional_usdc": 50.01},
        {"mainnet_codex_v1469_daily_soft_loss_usdc": 0.30},
        {"mainnet_codex_v1469_daily_hard_loss_usdc": 0.31},
        {"mainnet_codex_v1469_roundtrip_fee_bp": -0.01},
    ],
)
def test_v1469_enforcement_rejects_unsafe_contract(overrides):
    contract = {
        "mainnet_codex_v1469_observation_enabled": True,
        "mainnet_codex_v1469_paired_shadow_enabled": True,
        "mainnet_codex_v1469_arbiter_enabled": True,
        "mainnet_codex_v1469_live_enforcement_enabled": True,
        **overrides,
    }
    settings = _settings(
        mainnet_one_run_enabled=True,
        mainnet_codex_v1_enabled=True,
        mainnet_codex_v1462_strict_live_allowlist_enabled=True,
        mainnet_codex_v1462_shadow_all_enabled=True,
        mainnet_codex_v1462_promotion_enforcement_enabled=False,
        mainnet_codex_v1465_w6a_profile_enforcement_enabled=False,
        **contract,
    )

    with pytest.raises(RuntimeError, match="unsafe v1.4.64"):
        settings.assert_mainnet_v1463_runtime_safety()


def test_v1469_paired_shadow_requires_observation_even_without_enforcement():
    settings = _settings(
        mainnet_one_run_enabled=True,
        mainnet_codex_v1_enabled=True,
        mainnet_codex_v1462_strict_live_allowlist_enabled=True,
        mainnet_codex_v1462_shadow_all_enabled=True,
        mainnet_codex_v1462_promotion_enforcement_enabled=False,
        mainnet_codex_v1469_observation_enabled=False,
        mainnet_codex_v1469_paired_shadow_enabled=True,
    )

    with pytest.raises(RuntimeError, match="v1469_observation=true"):
        settings.assert_mainnet_v1463_runtime_safety()


@pytest.mark.parametrize(
    "overrides",
    [
        {"mainnet_codex_v1465_w6a_profile_shadow_enabled": False},
        {"mainnet_codex_v1465_w6a_profile_selector_enabled": False},
        {"mainnet_codex_v1464_auto_promotion_enabled": False},
        {"mainnet_codex_v1465_w6a_profile_lease_ttl_seconds": 601},
        {"mainnet_codex_v1465_w6a_profile_notional_cap_usdc": 25.01},
        {"mainnet_codex_v1465_w6a_profile_notional_cap_usdc": 0.0},
    ],
)
def test_v1465_w6a_enforcement_rejects_unsafe_contract(overrides):
    contract = {
        "mainnet_codex_v1464_auto_promotion_enabled": True,
        "mainnet_codex_v1465_w6a_profile_shadow_enabled": True,
        "mainnet_codex_v1465_w6a_profile_selector_enabled": True,
        "mainnet_codex_v1465_w6a_profile_lease_ttl_seconds": 600,
        "mainnet_codex_v1465_w6a_profile_notional_cap_usdc": 25.0,
        **overrides,
    }
    settings = _settings(
        mainnet_one_run_enabled=True,
        mainnet_codex_v1_enabled=True,
        mainnet_codex_v1462_strict_live_allowlist_enabled=True,
        mainnet_codex_v1462_shadow_all_enabled=True,
        mainnet_codex_v1462_promotion_enforcement_enabled=False,
        mainnet_codex_v1464_activation_cutoff_ms=1,
        mainnet_codex_v1465_w6a_profile_enforcement_enabled=True,
        **contract,
    )

    with pytest.raises(RuntimeError, match="unsafe v1.4.64"):
        settings.assert_mainnet_v1463_runtime_safety()


@pytest.mark.parametrize(
    "overrides",
    [
        {"mainnet_codex_v1461_runner_enabled": True},
        {"mainnet_codex_v1464_probation_notional_usdc": 25.01},
        {"mainnet_codex_v1464_live_notional_usdc": 50.01},
        {"mainnet_codex_v1464_live_min_paid_complete": 2},
        {"mainnet_codex_v1464_shadow_aggtrade_pages_per_cycle": 0},
        {"mainnet_codex_v1464_activation_cutoff_ms": 0},
        {"mainnet_codex_v1464_regime_max_age_seconds": 900},
        {"mainnet_codex_v1464_regime_confirmation_window_seconds": 61},
        {"mainnet_codex_v1464_submit_max_age_seconds": 61},
        {"mainnet_codex_v1464_max_terminal_latency_seconds": 5_401},
        {"mainnet_codex_recovery_enabled": True},
    ],
)
def test_v1464_auto_promotion_rejects_unsafe_contract(overrides):
    adaptive_contract = {
        "mainnet_codex_v1464_activation_cutoff_ms": 1,
        **overrides,
    }
    settings = _settings(
        mainnet_one_run_enabled=True,
        mainnet_codex_v1_enabled=True,
        mainnet_codex_v1462_strict_live_allowlist_enabled=True,
        mainnet_codex_v1462_shadow_all_enabled=True,
        mainnet_codex_v1462_promotion_enforcement_enabled=False,
        mainnet_codex_v1464_auto_promotion_enabled=True,
        **adaptive_contract,
    )

    with pytest.raises(RuntimeError, match="unsafe v1.4.64"):
        settings.assert_mainnet_v1463_runtime_safety()


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"mainnet_codex_v1_enabled": True},
        {
            "mainnet_one_run_enabled": True,
            "mainnet_strategy_label": "wildcat_v2_adverse_guard",
        },
    ],
)
def test_testnet_monitoring_and_non_codex_startups_are_unchanged(overrides):
    _settings(**overrides).assert_mainnet_v1463_runtime_safety()


@pytest.mark.asyncio
async def test_app_fails_before_database_or_network_side_effects():
    app = object.__new__(App)
    app.settings = _settings(
        mainnet_one_run_enabled=True,
        mainnet_codex_v1_enabled=True,
    )
    app.db = SimpleNamespace(initialize=AsyncMock())
    app.binance = SimpleNamespace(connect=AsyncMock())
    app.mainnet_binance = SimpleNamespace(connect=AsyncMock())

    with pytest.raises(RuntimeError, match="unsafe v1.4.64 mainnet Codex configuration"):
        await app.initialize()

    app.db.initialize.assert_not_awaited()
    app.binance.connect.assert_not_awaited()
    app.mainnet_binance.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_v1464_missing_schema_fails_before_network_connect():
    app = object.__new__(App)
    app.settings = _settings(
        mainnet_one_run_enabled=True,
        mainnet_codex_v1_enabled=True,
        mainnet_codex_v1462_strict_live_allowlist_enabled=True,
        mainnet_codex_v1462_shadow_all_enabled=True,
        mainnet_codex_v1462_promotion_enforcement_enabled=False,
        mainnet_codex_v1464_auto_promotion_enabled=True,
        mainnet_codex_v1464_activation_cutoff_ms=1,
    )
    app.db = SimpleNamespace(
        initialize=AsyncMock(),
    )
    app.v1464_promotion_repo = SimpleNamespace(
        assert_schema_ready=AsyncMock(
            side_effect=RuntimeError("missing promotion schema")
        )
    )
    app.binance = SimpleNamespace(connect=AsyncMock())
    app.mainnet_binance = SimpleNamespace(connect=AsyncMock())

    with pytest.raises(RuntimeError, match="promotion database schema"):
        await app.initialize()

    app.db.initialize.assert_awaited_once()
    app.v1464_promotion_repo.assert_schema_ready.assert_awaited_once()
    app.binance.connect.assert_not_awaited()
    app.mainnet_binance.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_v1465_missing_schema_fails_before_network_connect():
    app = object.__new__(App)
    app.settings = _settings(
        mainnet_one_run_enabled=True,
        mainnet_codex_v1_enabled=True,
        mainnet_codex_v1462_strict_live_allowlist_enabled=True,
        mainnet_codex_v1462_shadow_all_enabled=True,
        mainnet_codex_v1462_promotion_enforcement_enabled=False,
        mainnet_codex_v1464_auto_promotion_enabled=True,
        mainnet_codex_v1464_activation_cutoff_ms=1,
        mainnet_codex_v1465_w6a_profile_shadow_enabled=True,
        mainnet_codex_v1465_w6a_profile_selector_enabled=True,
        mainnet_codex_v1465_w6a_profile_enforcement_enabled=False,
    )
    app.db = SimpleNamespace(initialize=AsyncMock())
    app.v1464_promotion_repo = SimpleNamespace(
        assert_schema_ready=AsyncMock(return_value="v1464-fingerprint")
    )
    app.v1465_w6a_profile_repo = SimpleNamespace(
        assert_schema_ready=AsyncMock(
            side_effect=RuntimeError("missing W6A profile schema")
        )
    )
    app.binance = SimpleNamespace(connect=AsyncMock())
    app.mainnet_binance = SimpleNamespace(connect=AsyncMock())

    with pytest.raises(RuntimeError, match="W6A profile database schema"):
        await app.initialize()

    app.db.initialize.assert_awaited_once()
    app.v1464_promotion_repo.assert_schema_ready.assert_awaited_once()
    app.v1465_w6a_profile_repo.assert_schema_ready.assert_awaited_once()
    app.binance.connect.assert_not_awaited()
    app.mainnet_binance.connect.assert_not_awaited()
