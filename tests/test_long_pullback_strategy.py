import unittest

from src.gridbot.strategy.long_pullback import (
    Candle,
    StrategyConfig,
    _daily_guard_reason,
    _position_sizing,
    _risk_adjusted_config,
    generate_signal,
    run_backtest,
    sweep_configs,
)


def make_candles(count: int = 140, start: float = 2300.0) -> list[Candle]:
    candles: list[Candle] = []
    price = start
    for index in range(count):
        if index < 90:
            price += 0.6
        elif index < 118:
            price -= 0.75
        else:
            price += 0.25
        high = price + 1.4
        low = price - 1.4
        candles.append(Candle(
            open_time_ms=1_700_000_000_000 + index * 300_000,
            open=price - 0.2,
            high=high,
            low=low,
            close=price,
            volume=100 + index,
            quote_volume=(100 + index) * price,
        ))
    return candles


class TestLongPullbackStrategy(unittest.TestCase):
    def test_signal_produces_long_plan_on_medium_pullback(self):
        config = StrategyConfig(min_score=45)
        signal = generate_signal(make_candles(), config)
        self.assertIn(signal.action, {"PLAN_LONG", "WAIT"})
        self.assertGreaterEqual(signal.price, 0)
        if signal.action == "PLAN_LONG":
            self.assertEqual(len(signal.entries), 3)
            self.assertLess(signal.stop_loss, signal.entries[-1])
            self.assertGreater(signal.planned_notional_usdc, 0)

    def test_fast_drop_waits(self):
        candles = make_candles()
        last = candles[-1]
        candles.extend([
            Candle(last.open_time_ms + 300_000, last.close, last.close + 0.5, last.close - 12, last.close - 10, 100),
            Candle(last.open_time_ms + 600_000, last.close - 10, last.close - 9, last.close - 24, last.close - 22, 100),
            Candle(last.open_time_ms + 900_000, last.close - 22, last.close - 21, last.close - 35, last.close - 34, 100),
        ])
        signal = generate_signal(candles, StrategyConfig(min_score=30))
        self.assertEqual(signal.action, "WAIT")
        self.assertTrue(any("fast drop" in reason for reason in signal.reasons))

    def test_backtest_returns_summary(self):
        summary = run_backtest(make_candles(220), StrategyConfig(min_score=40))
        self.assertGreaterEqual(summary.total_trades, 0)
        self.assertIsInstance(summary.net_pnl_usdc, float)
        self.assertIsInstance(summary.daily_pnls, dict)

    def test_sweep_returns_sorted_results(self):
        results = sweep_configs(make_candles(180), StrategyConfig())
        self.assertGreater(len(results), 0)
        self.assertTrue(all(result.config.equity_usdc == 200.0 for result in results))

    def test_aggressive_sweep_profile_runs(self):
        results = sweep_configs(make_candles(180), StrategyConfig(), profile="aggressive")
        self.assertGreater(len(results), 0)
        self.assertTrue(any(result.config.risk_per_trade_pct >= 1.5 for result in results))

    def test_daily_guards_detect_target_and_loss_limits(self):
        config = StrategyConfig()
        self.assertIsNone(_daily_guard_reason(config, config.daily_target_stop_usdc - 0.01))
        self.assertEqual(_daily_guard_reason(config, config.daily_target_stop_usdc), "daily target reached")
        self.assertEqual(_daily_guard_reason(config, -config.daily_max_loss_usdc), "daily max loss reached")

    def test_soft_daily_loss_scales_risk_without_hard_stop(self):
        config = StrategyConfig(risk_per_trade_pct=2.0, accelerator_risk_per_trade_pct=1.0)
        scaled = _risk_adjusted_config(config, -config.daily_soft_loss_usdc)
        self.assertLess(scaled.risk_per_trade_pct, config.risk_per_trade_pct)
        self.assertLess(scaled.accelerator_risk_per_trade_pct, config.accelerator_risk_per_trade_pct)
        self.assertIsNone(_daily_guard_reason(config, -config.daily_soft_loss_usdc))

    def test_accelerator_sizing_uses_small_margin_cap(self):
        notes: list[str] = []
        config = StrategyConfig(
            accelerator_min_score=80,
            accelerator_margin_pct=8.0,
            accelerator_max_effective_leverage=30.0,
            accelerator_risk_per_trade_pct=0.35,
        )
        sizing = _position_sizing(100.0, 99.0, 90, config, notes)
        self.assertEqual(sizing.sizing_mode, "core+accelerator")
        self.assertLessEqual(
            sizing.planned_margin_usdc,
            config.equity_usdc * (config.max_position_margin_pct + config.accelerator_margin_pct) / 100,
        )
        self.assertTrue(any("accelerator add-on" in note for note in notes))


if __name__ == "__main__":
    unittest.main()
