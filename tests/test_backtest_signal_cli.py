import argparse
import unittest
from unittest.mock import patch

from scripts.backtest_signal import (
    _apply_preset,
    _parse_float_tuple,
    _resolve_timerange,
    build_portfolio_orb_config_from_args,
    build_strategy_config_from_args,
    fetch_klines,
)


class TestBacktestSignalCli(unittest.TestCase):
    def test_resolve_timerange_from_dates(self):
        start_ms, end_ms = _resolve_timerange(30, "2026-02-01", "2026-02-28")
        self.assertEqual(start_ms, 1_769_904_000_000)
        self.assertEqual(end_ms, 1_772_323_200_000)

    def test_resolve_timerange_requires_both_dates(self):
        with self.assertRaises(ValueError):
            _resolve_timerange(30, "2026-02-01", None)

    def test_builds_full_portfolio_orb_config_from_cli_args(self):
        args = argparse.Namespace(
            symbol="ETHUSDC",
            equity=200.0,
            compounding=True,
            daily_target_min_pct=3.0,
            daily_target_max_pct=3.0,
            risk=4.2,
            min_score=44,
            max_leverage=35.0,
            maker_fee=None,
            taker_fee=0.0004,
            daily_soft_loss_pct=4.5,
            daily_max_loss_pct=10.0,
            daily_loss_risk_scale=0.65,
            daily_target_stop_pct=3.0,
            keep_trading_after_target=False,
            max_open_positions=1,
            max_position_margin_pct=60.0,
            cooldown_bars=6,
            loss_cooldown_after=3,
            loss_cooldown_bars=18,
            max_holding_bars=48,
            take_profit_r="0.55,1.1,2.2",
            entry_weights="0.40,0.35,0.25",
            exit_weights="0.25,0.35,0.40",
            disable_accelerator=False,
            accelerator_min_score=85,
            accelerator_risk=0.35,
            accelerator_margin_pct=8.0,
            accelerator_max_leverage=30.0,
            orb_session_start_bar=0,
            orb_opening_range_bars=9,
            orb_min_volume_ratio=0.8,
            orb_stop_atr=0.6,
            portfolio_max_concurrent_positions=3,
            portfolio_margin_cap_pct=100.0,
            portfolio_require_benchmark_trend=False,
            portfolio_benchmark_risk_scale=0.7,
            portfolio_soft_regime_floor=3,
            portfolio_hard_regime_floor=0,
            portfolio_weak_max_positions=1,
            portfolio_previous_loss_risk_scale=0.55,
            portfolio_previous_loss_max_positions=1,
            portfolio_allow_short=False,
            portfolio_short_risk_scale=0.75,
            portfolio_short_regime_max_score=2,
            portfolio_allow_reversion=False,
            portfolio_reversion_risk_scale=0.45,
            portfolio_reversion_regime_max_score=3,
            portfolio_reversion_min_deviation_atr=1.2,
            portfolio_reversion_min_wick_ratio=0.35,
            portfolio_reversion_max_trades_per_day=1,
            portfolio_selector_enabled=False,
            portfolio_selector_min_score=0,
            portfolio_selector_strong_score=7,
            portfolio_selector_strong_risk_scale=1.0,
            portfolio_selector_min_orb_width_atr=0.35,
            portfolio_selector_max_orb_width_atr=6.0,
            portfolio_rolling_loss_lookback_days=2,
            portfolio_rolling_loss_pause_pct=8.0,
            portfolio_high_conviction_score=88,
            portfolio_high_conviction_weight=1.6,
            portfolio_ai_regime_enabled=True,
            portfolio_ai_regime_block_enabled=False,
            portfolio_ai_regime_block_regimes="trend_down",
            portfolio_ai_regime_min_confidence=0.65,
            portfolio_ai_regime_small_risk_scale=0.4,
            portfolio_ai_regime_aggressive_risk_scale=1.15,
        )

        base = build_strategy_config_from_args(args)
        portfolio = build_portfolio_orb_config_from_args(args, base, ("ETHUSDC", "BTCUSDC", "SOLUSDC"))

        self.assertEqual(base.take_profit_r, (0.55, 1.1, 2.2))
        self.assertEqual(base.exit_weights, (0.25, 0.35, 0.40))
        self.assertEqual(base.max_holding_bars, 48)
        self.assertEqual(base.cooldown_bars, 6)
        self.assertEqual(portfolio.per_symbol.opening_range_bars, 9)
        self.assertEqual(portfolio.per_symbol.min_volume_ratio, 0.8)
        self.assertEqual(portfolio.per_symbol.stop_atr, 0.6)
        self.assertEqual(portfolio.rolling_loss_lookback_days, 2)
        self.assertEqual(portfolio.rolling_loss_pause_pct, 8.0)
        self.assertTrue(portfolio.ai_regime_enabled)
        self.assertFalse(portfolio.ai_regime_block_enabled)
        self.assertEqual(portfolio.ai_regime_block_regimes, ("trend_down",))
        self.assertEqual(portfolio.ai_regime_min_confidence, 0.65)

    def test_parse_weights_requires_sum_to_one(self):
        with self.assertRaises(ValueError):
            _parse_float_tuple("0.5,0.5,0.5", 3, "--exit-weights")

    def test_orb_3pct_preset_sets_candidate_parameters(self):
        args = argparse.Namespace(preset="orb_3pct_v1", strategy="pullback", compounding=False)

        _apply_preset(args)

        self.assertEqual(args.strategy, "portfolio_orb")
        self.assertTrue(args.compounding)
        self.assertEqual(args.risk, 4.2)
        self.assertEqual(args.cooldown_bars, 6)
        self.assertEqual(args.max_holding_bars, 48)
        self.assertEqual(args.take_profit_r, "0.55,1.1,2.2")
        self.assertEqual(args.portfolio_rolling_loss_lookback_days, 2)
        self.assertEqual(args.portfolio_rolling_loss_pause_pct, 8.0)
        self.assertFalse(args.portfolio_ai_regime_enabled)
        self.assertFalse(args.portfolio_ai_regime_block_enabled)
        self.assertEqual(args.portfolio_ai_regime_block_regimes, "")
        self.assertEqual(args.portfolio_ai_regime_min_confidence, 0.60)
        self.assertEqual(args.orb_opening_range_bars, 9)

    def test_orb_3pct_preset_allows_explicit_overrides(self):
        args = argparse.Namespace(
            preset="orb_3pct_v1",
            strategy="pullback",
            risk=1.0,
            portfolio_selector_enabled=True,
        )

        _apply_preset(args, ["--preset", "orb_3pct_v1", "--risk", "1.0", "--portfolio-selector-enabled"])

        self.assertEqual(args.strategy, "portfolio_orb")
        self.assertEqual(args.risk, 1.0)
        self.assertTrue(args.portfolio_selector_enabled)

    def test_fetch_klines_excludes_end_boundary_row(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return [
                    [1000, "10", "11", "9", "10.5", "100", 0, "1000"],
                    [2000, "10", "11", "9", "10.5", "100", 0, "1000"],
                ]

        with patch("scripts.backtest_signal.requests.get", return_value=Response()):
            candles = fetch_klines("https://example.test", "ETHUSDC", "5m", 1000, 2000)

        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].open_time_ms, 1000)


if __name__ == "__main__":
    unittest.main()
