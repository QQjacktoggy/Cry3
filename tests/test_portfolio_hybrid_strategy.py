import unittest

from src.gridbot.strategy.long_pullback import Candle, StrategyConfig
from src.gridbot.strategy.portfolio_hybrid import (
    PortfolioHybridConfig,
    run_portfolio_hybrid_backtest,
    sweep_portfolio_hybrid_configs,
)


def make_symbol_candles(count: int, start: float, bias: float) -> list[Candle]:
    candles: list[Candle] = []
    price = start
    for index in range(count):
        if index < 80:
            price += bias * 0.30
            volume = 100
        elif index < 120:
            price += bias * 0.95
            volume = 125
        elif index < 150:
            price -= bias * 0.40
            volume = 110
        else:
            price += bias * 1.10
            volume = 175
        candles.append(Candle(
            open_time_ms=1_700_000_000_000 + index * 300_000,
            open=price - 0.3,
            high=price + 0.9,
            low=price - 0.9,
            close=price,
            volume=volume,
            quote_volume=volume * price,
        ))
    return candles


class TestPortfolioHybridStrategy(unittest.TestCase):
    def test_backtest_runs(self):
        candles_by_symbol = {
            "ETHUSDC": make_symbol_candles(220, 2300.0, 1.0),
            "BTCUSDC": make_symbol_candles(220, 64000.0, 0.8),
            "SOLUSDC": make_symbol_candles(220, 140.0, 1.3),
        }
        summary = run_portfolio_hybrid_backtest(
            candles_by_symbol,
            PortfolioHybridConfig(base=StrategyConfig()),
        )
        self.assertGreaterEqual(summary.total_trades, 0)
        self.assertIsInstance(summary.net_pnl_usdc, float)

    def test_sweep_runs(self):
        candles_by_symbol = {
            "ETHUSDC": make_symbol_candles(220, 2300.0, 1.0),
            "BTCUSDC": make_symbol_candles(220, 64000.0, 0.8),
            "SOLUSDC": make_symbol_candles(220, 140.0, 1.3),
        }
        results = sweep_portfolio_hybrid_configs(candles_by_symbol, StrategyConfig())
        self.assertGreater(len(results), 0)
        self.assertTrue(any(result.config.risk_per_trade_pct >= 1.4 for result in results))


if __name__ == "__main__":
    unittest.main()
