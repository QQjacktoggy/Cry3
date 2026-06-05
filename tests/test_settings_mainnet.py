from config.settings import Settings


def _settings(**overrides) -> Settings:
    return Settings.model_construct(
        binance_api_key="x",
        binance_api_secret="y",
        **overrides,
    )


def test_mainnet_effective_entry_notional_is_clamped_to_equity_cap():
    settings = _settings(
        mainnet_equity_cap_usdc=200.0,
        mainnet_initial_notional_usdc=1000.0,
        mainnet_leverage=75,
    )

    assert settings.mainnet_effective_entry_notional_usdc == 200.0
    assert settings.mainnet_effective_entry_margin_usdc == 200.0 / 75.0


def test_mainnet_effective_cumulative_notional_respects_recovery_steps():
    settings = _settings(
        mainnet_equity_cap_usdc=200.0,
        mainnet_initial_notional_usdc=200.0,
        mainnet_max_cumulative_notional_usdc=800.0,
        mainnet_recovery_steps=3,
    )

    assert settings.mainnet_effective_max_cumulative_notional_usdc == 800.0
