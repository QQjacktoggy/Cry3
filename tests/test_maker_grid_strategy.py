from src.gridbot.strategy.long_pullback import Candle, StrategyConfig
from src.gridbot.strategy.maker_grid import MakerGridConfig, run_maker_grid_backtest


def _range_candles(count: int = 180) -> list[Candle]:
    candles = []
    base_ms = 1_700_000_000_000
    for index in range(count):
        center = 100.0 + (index % 12 - 6) * 0.08
        candles.append(
            Candle(
                open_time_ms=base_ms + index * 300_000,
                open=center,
                high=center + 0.45,
                low=center - 0.45,
                close=center + (0.12 if index % 2 == 0 else -0.12),
                volume=100.0,
                quote_volume=10_000.0,
            )
        )
    return candles


def test_maker_grid_backtest_trades_range_with_maker_take_profit():
    config = MakerGridConfig(
        base=StrategyConfig(
            symbol="ETHUSDC",
            equity_usdc=200.0,
            risk_per_trade_pct=3.0,
            max_effective_leverage=20.0,
            max_position_margin_pct=30.0,
            cooldown_bars=1,
            daily_target_stop_pct=100.0,
        ),
        spacing_atr=0.05,
        take_profit_atr=0.10,
        stop_atr=1.0,
        min_range_width_atr=0.5,
        max_ema_spread_atr=2.0,
    )

    summary = run_maker_grid_backtest(_range_candles(), config)

    assert summary.total_trades > 0
    assert any(trade.reason.endswith("take_profit") for trade in summary.trades)
    assert all(trade.fees_usdc >= 0 for trade in summary.trades)


def test_maker_grid_respects_side_filter():
    short_config = MakerGridConfig(
        base=StrategyConfig(symbol="ETHUSDC", daily_target_stop_pct=100.0),
        side="short",
        spacing_atr=0.05,
        take_profit_atr=0.10,
        stop_atr=1.0,
        min_range_width_atr=0.5,
        max_ema_spread_atr=2.0,
    )

    summary = run_maker_grid_backtest(_range_candles(), short_config)

    assert summary.total_trades > 0
    assert all(trade.reason.startswith("grid_short") for trade in summary.trades)
