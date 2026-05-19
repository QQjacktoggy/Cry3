import unittest
from dataclasses import replace

from src.gridbot.strategy.long_pullback import BacktestSummary, StrategyConfig, TradeResult
from src.gridbot.strategy.regime_attribution import attribute_trades_by_regime, summarize_regime_attribution
from src.gridbot.strategy.regime import (
    build_regime_context,
    classify_regime,
)
from tests.test_long_orb_strategy import make_orb_candles


class TestRegimeStrategy(unittest.TestCase):
    def test_classifier_uses_bounded_schema(self):
        candles = make_orb_candles(days=5)
        context = build_regime_context(candles)

        decision = classify_regime(candles, 360, context)

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertIn(decision.regime, {"trend_up", "trend_down", "range", "high_volatility", "low_liquidity", "chop"})
        self.assertIn(decision.risk_mode, {"off", "small", "normal", "aggressive"})
        self.assertGreaterEqual(decision.confidence, 0.0)
        self.assertLessEqual(decision.confidence, 1.0)

    def test_classifier_does_not_look_ahead(self):
        candles = make_orb_candles(days=5)
        index = 360
        context = build_regime_context(candles)
        decision = classify_regime(candles, index, context)

        mutated = list(candles)
        for future_index in range(index + 1, len(mutated)):
            candle = mutated[future_index]
            mutated[future_index] = replace(
                candle,
                open=candle.open * 3,
                high=candle.high * 3,
                low=candle.low * 3,
                close=candle.close * 3,
                volume=candle.volume * 20,
            )
        mutated_context = build_regime_context(mutated)
        mutated_decision = classify_regime(mutated, index, mutated_context)

        self.assertEqual(decision, mutated_decision)

    def test_trade_attribution_uses_signal_candle_before_entry(self):
        candles = make_orb_candles(days=5)
        entry_index = 361
        summary = BacktestSummary(
            config=StrategyConfig(),
            trades=[
                TradeResult(
                    entry_time_ms=candles[entry_index].open_time_ms,
                    exit_time_ms=candles[entry_index + 6].open_time_ms,
                    entry_price=candles[entry_index].open,
                    exit_price=candles[entry_index + 6].close,
                    qty=1.0,
                    pnl_usdc=5.0,
                    fees_usdc=0.1,
                    r_multiple=1.2,
                    reason="take_profit_1",
                    hold_bars=6,
                )
            ],
            net_pnl_usdc=5.0,
            return_pct=2.5,
            max_drawdown_usdc=0.0,
            max_drawdown_pct=0.0,
            win_rate_pct=100.0,
            profit_factor=float("inf"),
            expectancy_usdc=5.0,
            max_consecutive_losses=0,
            avg_daily_return_pct=0.5,
            daily_target_min_hit_rate_pct=0.0,
            daily_target_max_hit_rate_pct=0.0,
            daily_pnls={},
        )

        rows = attribute_trades_by_regime(summary, candles)
        mutated = list(candles)
        for future_index in range(entry_index, len(mutated)):
            candle = mutated[future_index]
            mutated[future_index] = replace(
                candle,
                open=candle.open * 2,
                high=candle.high * 2,
                low=candle.low * 2,
                close=candle.close * 2,
                volume=candle.volume * 10,
            )
        mutated_rows = attribute_trades_by_regime(summary, mutated)

        self.assertEqual(rows, mutated_rows)
        bucket = summarize_regime_attribution(rows)
        self.assertEqual(sum(row["trades"] for row in bucket), 1)


if __name__ == "__main__":
    unittest.main()
