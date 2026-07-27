from config.settings import Settings


PROFILE_FLAG_NAMES = (
    "mainnet_codex_v1459_candidate_selector_enabled",
    "mainnet_codex_v1459_live_enforcement_enabled",
    "mainnet_codex_v1459_runner_enabled",
    "mainnet_codex_v1459_early_fail_enabled",
    "mainnet_codex_v1459_one_step_reprice_enabled",
    "mainnet_codex_v1459_regime_switch_enabled",
)


PROFILE_TUNABLE_DEFAULTS = {
    "mainnet_codex_v1459_regime_confirmations": 2,
    "mainnet_codex_v1459_regime_min_dwell_seconds": 15,
    "mainnet_codex_v1459_regime_stale_after_seconds": 90,
    "mainnet_codex_v1459_regime_max_notional_usdc": 50.0,
    "mainnet_codex_v1459_trend_size_mult": 1.0,
    "mainnet_codex_v1459_trend_entry_offset_bp": 2.0,
    "mainnet_codex_v1459_trend_tp1_bp": 6.0,
    "mainnet_codex_v1459_trend_full_tp_bp": 16.0,
    "mainnet_codex_v1459_trend_partial_exit_pct": 0.70,
    "mainnet_codex_v1459_trend_sl_bp": 10.0,
    "mainnet_codex_v1459_trend_be_bp": 2.0,
    "mainnet_codex_v1459_trend_entry_ttl_seconds": 60,
    "mainnet_codex_v1459_trend_hold_seconds": 720,
    "mainnet_codex_v1459_range_size_mult": 0.75,
    "mainnet_codex_v1459_range_entry_offset_bp": 1.0,
    "mainnet_codex_v1459_range_tp1_bp": 5.0,
    "mainnet_codex_v1459_range_full_tp_bp": 8.0,
    "mainnet_codex_v1459_range_partial_exit_pct": 1.0,
    "mainnet_codex_v1459_range_sl_bp": 8.0,
    "mainnet_codex_v1459_range_be_bp": 2.0,
    "mainnet_codex_v1459_range_entry_ttl_seconds": 90,
    "mainnet_codex_v1459_range_hold_seconds": 360,
}

def test_v1459_profile_runtime_flags_use_observation_stage_defaults() -> None:
    settings = Settings(
        binance_api_key="test",
        binance_api_secret="test",
        _env_file=None,
    )

    assert settings.mainnet_v1459_observation_enabled is True
    assert settings.mainnet_v1459_observation_persist_session_enabled is True
    assert settings.mainnet_v1459_observation_record_opportunities_enabled is True
    assert settings.mainnet_v1459_observation_record_shadow_enabled is True
    assert settings.mainnet_v1459_observation_record_reconciliation_enabled is True
    assert settings.mainnet_codex_v1459_candidate_selector_enabled is True
    assert all(
        getattr(settings, name) is False
        for name in PROFILE_FLAG_NAMES
        if name != "mainnet_codex_v1459_candidate_selector_enabled"
    )
    assert {
        name: getattr(settings, name) for name in PROFILE_TUNABLE_DEFAULTS
    } == PROFILE_TUNABLE_DEFAULTS

def test_v1459_profile_runtime_flags_can_be_explicitly_enabled() -> None:
    settings = Settings(
        binance_api_key="test",
        binance_api_secret="test",
        mainnet_codex_v1459_candidate_selector_enabled=True,
        mainnet_codex_v1459_live_enforcement_enabled=True,
        mainnet_codex_v1459_runner_enabled=True,
        mainnet_codex_v1459_early_fail_enabled=True,
        mainnet_codex_v1459_one_step_reprice_enabled=True,
        mainnet_codex_v1459_regime_switch_enabled=True,
        **{
            name: value + 1 if isinstance(value, int) else value + 0.1
            for name, value in PROFILE_TUNABLE_DEFAULTS.items()
        },
        _env_file=None,
    )

    assert all(getattr(settings, name) is True for name in PROFILE_FLAG_NAMES)
    assert all(
        getattr(settings, name)
        == (value + 1 if isinstance(value, int) else value + 0.1)
        for name, value in PROFILE_TUNABLE_DEFAULTS.items()
    )
