from src.gridbot.strategy.fakeout_reversal import (
    FakeoutReversalConfig,
    generate_fakeout_reversal_signal_at,
    run_fakeout_reversal_backtest,
)
from src.gridbot.strategy.long_orb import OrbConfig, build_orb_context
from src.gridbot.strategy.long_pullback import Candle, StrategyConfig


def _fakeout_candles(side: str = "short") -> list[Candle]:
    candles = []
    base_ms = 1_704_067_200_000
    for index in range(80):
        if index < 5:
            open_, high, low, close = 100.0, 101.0, 99.0, 100.0
        elif index == 24 and side == "short":
            open_, high, low, close = 100.8, 101.9, 100.4, 100.7
        elif index == 24:
            open_, high, low, close = 99.2, 99.6, 98.1, 99.3
        elif index == 25:
            open_, high, low, close = 100.7, 101.0, 99.8, 100.0
        else:
            drift = (index % 7 - 3) * 0.04
            open_, high, low, close = 100.0 + drift, 100.8 + drift, 99.2 + drift, 100.0 + drift
        candles.append(
            Candle(
                open_time_ms=base_ms + index * 300_000,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=100.0,
                quote_volume=10_000.0,
            )
        )
    return candles


def _config() -> FakeoutReversalConfig:
    base = StrategyConfig(
        symbol="ETHUSDC",
        equity_usdc=200.0,
        risk_per_trade_pct=2.0,
        max_effective_leverage=20.0,
        max_position_margin_pct=40.0,
        min_score=40,
        atr_period=5,
        ema_fast_period=5,
        ema_slow_period=10,
        rsi_period=5,
        vwap_period=10,
        cooldown_bars=1,
        daily_target_stop_pct=100.0,
    )
    return FakeoutReversalConfig(
        orb=OrbConfig(base=base, opening_range_bars=5, volume_lookback=10, min_orb_range_atr=0.2),
        min_probe_atr=0.05,
        min_wick_ratio=0.25,
        min_volume_ratio=0.5,
        min_orb_width_atr=0.2,
        max_orb_width_atr=5.0,
        reject_strong_trend=False,
    )


def test_fakeout_reversal_short_signal_shape():
    candles = _fakeout_candles("short")
    config = _config()
    context = build_orb_context(candles, config.orb)

    signal = generate_fakeout_reversal_signal_at(candles, 24, config, context)

    assert signal.action == "PLAN_SHORT"
    assert signal.stop_loss > signal.entries[0]
    assert signal.take_profits[0] < signal.entries[0]


def test_fakeout_reversal_backtest_can_close_trade():
    summary = run_fakeout_reversal_backtest(_fakeout_candles("short"), _config())

    assert summary.total_trades > 0
    assert any(trade.reason.startswith("take_profit") for trade in summary.trades)
