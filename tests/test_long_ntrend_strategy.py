import unittest

from src.gridbot.strategy.long_ntrend import (
    NTrendConfig,
    generate_ntrend_signal,
    run_ntrend_backtest,
    sweep_ntrend_configs,
)
from src.gridbot.strategy.long_pullback import Candle, StrategyConfig


def make_ntrend_candles(count: int = 180, start: float = 2300.0) -> list[Candle]:
    candles: list[Candle] = []
    price = start
    for index in range(count):
        if index < 40:
            price += 0.25
            volume = 95
        elif index < 70:
            price += 1.0
            volume = 120
        elif index < 100:
            price -= 0.45
            volume = 110
        elif index < 130:
            price += 0.15
            volume = 105
        else:
            price += 1.15
            volume = 170
        candles.append(Candle(
            open_time_ms=1_700_000_000_000 + index * 300_000,
            open=price - 0.45,
            high=price + 0.9,
            low=price - 0.9,
            close=price,
            volume=volume,
            quote_volume=volume * price,
        ))
    return candles


class TestLongNTrendStrategy(unittest.TestCase):
    def test_signal_shape(self):
        signal = generate_ntrend_signal(
            make_ntrend_candles(),
            NTrendConfig(base=StrategyConfig(min_score=45)),
        )
        self.assertIn(signal.action, {"PLAN_LONG", "WAIT"})
        self.assertGreater(signal.price, 0)
        if signal.action == "PLAN_LONG":
            self.assertEqual(len(signal.entries), 1)
            self.assertLess(signal.stop_loss, signal.entries[0])

    def test_backtest_runs(self):
        summary = run_ntrend_backtest(
            make_ntrend_candles(220),
            NTrendConfig(base=StrategyConfig(min_score=45)),
        )
        self.assertGreaterEqual(summary.total_trades, 0)
        self.assertIsInstance(summary.net_pnl_usdc, float)

    def test_sweep_runs(self):
        results = sweep_ntrend_configs(
            make_ntrend_candles(220),
            StrategyConfig(),
            profile="aggressive",
        )
        self.assertGreater(len(results), 0)
        self.assertTrue(any(result.config.risk_per_trade_pct >= 1.8 for result in results))


if __name__ == "__main__":
    unittest.main()
