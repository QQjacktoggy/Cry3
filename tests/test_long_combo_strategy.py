import unittest

from src.gridbot.strategy.long_breakout import BreakoutConfig
from src.gridbot.strategy.long_combo import ComboConfig, generate_combo_signal, run_combo_backtest, sweep_combo_configs
from src.gridbot.strategy.long_pullback import Candle, StrategyConfig


def make_combo_candles(count: int = 220, start: float = 2300.0) -> list[Candle]:
    candles: list[Candle] = []
    price = start
    for index in range(count):
        if index < 80:
            price += 0.45
            volume = 110
        elif index < 120:
            price -= 0.75
            volume = 105
        elif index < 160:
            price += 0.25
            volume = 115
        else:
            price += 1.1
            volume = 180
        candles.append(Candle(
            open_time_ms=1_700_000_000_000 + index * 300_000,
            open=price - 0.35,
            high=price + 1.1,
            low=price - 1.1,
            close=price,
            volume=volume,
            quote_volume=volume * price,
        ))
    return candles


class TestLongComboStrategy(unittest.TestCase):
    def test_signal_runs(self):
        signal = generate_combo_signal(
            make_combo_candles(),
            ComboConfig(base=StrategyConfig(min_score=40), breakout=BreakoutConfig()),
        )
        self.assertIn(signal.action, {"PLAN_LONG", "WAIT"})
        self.assertGreaterEqual(signal.price, 0)

    def test_backtest_runs(self):
        summary = run_combo_backtest(
            make_combo_candles(),
            ComboConfig(base=StrategyConfig(min_score=40), breakout=BreakoutConfig()),
        )
        self.assertGreaterEqual(summary.total_trades, 0)
        self.assertIsInstance(summary.net_pnl_usdc, float)

    def test_spec_sweep_runs(self):
        results = sweep_combo_configs(make_combo_candles(), StrategyConfig(), profile="spec")
        self.assertGreater(len(results), 0)
        self.assertTrue(any(result.config.risk_per_trade_pct >= 4.0 for result in results))


if __name__ == "__main__":
    unittest.main()
