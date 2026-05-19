import unittest

from src.gridbot.strategy.long_hybrid import (
    HybridConfig,
    generate_hybrid_signal,
    run_hybrid_backtest,
    sweep_hybrid_configs,
)
from src.gridbot.strategy.long_pullback import Candle, StrategyConfig


def make_hybrid_candles(count: int = 200, start: float = 2300.0) -> list[Candle]:
    candles: list[Candle] = []
    price = start
    for index in range(count):
        if index < 60:
            price += 0.35
            volume = 100
        elif index < 95:
            price += 0.95
            volume = 130
        elif index < 125:
            price -= 0.45
            volume = 105
        elif index < 145:
            price += 0.10
            volume = 110
        else:
            price += 1.20
            volume = 180
        candles.append(Candle(
            open_time_ms=1_700_000_000_000 + index * 300_000,
            open=price - 0.4,
            high=price + 0.85,
            low=price - 0.85,
            close=price,
            volume=volume,
            quote_volume=volume * price,
        ))
    return candles


class TestLongHybridStrategy(unittest.TestCase):
    def test_signal_shape(self):
        signal = generate_hybrid_signal(
            make_hybrid_candles(),
            HybridConfig(base=StrategyConfig(min_score=45)),
        )
        self.assertIn(signal.action, {"PLAN_LONG", "WAIT"})
        self.assertGreater(signal.price, 0)

    def test_backtest_runs(self):
        summary = run_hybrid_backtest(
            make_hybrid_candles(220),
            HybridConfig(base=StrategyConfig(min_score=45)),
        )
        self.assertGreaterEqual(summary.total_trades, 0)
        self.assertIsInstance(summary.net_pnl_usdc, float)

    def test_sweep_runs(self):
        results = sweep_hybrid_configs(
            make_hybrid_candles(220),
            StrategyConfig(),
            profile="aggressive",
        )
        self.assertGreater(len(results), 0)
        self.assertTrue(any(result.config.risk_per_trade_pct >= 1.4 for result in results))


if __name__ == "__main__":
    unittest.main()
