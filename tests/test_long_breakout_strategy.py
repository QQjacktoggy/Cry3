import unittest

from src.gridbot.strategy.long_breakout import (
    BreakoutConfig,
    build_breakout_context_with_derivatives,
    generate_breakout_signal,
    generate_breakout_signal_at,
    run_breakout_backtest,
    sweep_breakout_configs,
)
from src.gridbot.strategy.long_pullback import Candle, StrategyConfig


def make_breakout_candles(count: int = 160, start: float = 2300.0) -> list[Candle]:
    candles: list[Candle] = []
    price = start
    for index in range(count):
        if index < 110:
            price += 0.15
            volume = 100
        elif index < 130:
            price += 0.4
            volume = 120
        else:
            price += 1.2
            volume = 180
        candles.append(Candle(
            open_time_ms=1_700_000_000_000 + index * 300_000,
            open=price - 0.4,
            high=price + 0.8,
            low=price - 0.8,
            close=price,
            volume=volume,
            quote_volume=volume * price,
        ))
    return candles


class TestLongBreakoutStrategy(unittest.TestCase):
    def test_signal_shape(self):
        signal = generate_breakout_signal(
            make_breakout_candles(),
            BreakoutConfig(base=StrategyConfig(min_score=45)),
        )
        self.assertIn(signal.action, {"PLAN_LONG", "WAIT"})
        self.assertGreater(signal.price, 0)
        if signal.action == "PLAN_LONG":
            self.assertEqual(len(signal.entries), 1)
            self.assertLess(signal.stop_loss, signal.entries[0])

    def test_backtest_runs(self):
        summary = run_breakout_backtest(
            make_breakout_candles(220),
            BreakoutConfig(base=StrategyConfig(min_score=45)),
        )
        self.assertGreaterEqual(summary.total_trades, 0)
        self.assertIsInstance(summary.net_pnl_usdc, float)

    def test_sweep_runs(self):
        results = sweep_breakout_configs(
            make_breakout_candles(220),
            StrategyConfig(),
            profile="aggressive",
        )
        self.assertGreater(len(results), 0)
        self.assertTrue(any(result.config.risk_per_trade_pct >= 1.5 for result in results))

    def test_oi_and_funding_filters_gate_signal(self):
        candles = make_breakout_candles()
        config = BreakoutConfig(
            base=StrategyConfig(min_score=45),
            require_oi_confirmation=True,
            min_oi_delta_pct=0.5,
            reject_extreme_funding=True,
            max_funding_rate=0.0003,
        )
        weak_context = build_breakout_context_with_derivatives(
            candles,
            config,
            oi_delta_pct_values=[0.1] * len(candles),
            funding_rate_values=[0.0001] * len(candles),
        )
        weak_signal = generate_breakout_signal_at(candles, len(candles) - 1, config, weak_context)
        self.assertEqual(weak_signal.action, "WAIT")

        hot_context = build_breakout_context_with_derivatives(
            candles,
            config,
            oi_delta_pct_values=[1.0] * len(candles),
            funding_rate_values=[0.0005] * len(candles),
        )
        hot_signal = generate_breakout_signal_at(candles, len(candles) - 1, config, hot_context)
        self.assertEqual(hot_signal.action, "WAIT")


if __name__ == "__main__":
    unittest.main()
