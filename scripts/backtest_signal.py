"""CLI for the long-only ETH pullback signal/backtest engine."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.gridbot.strategy.long_pullback import (
    Candle,
    StrategyConfig,
    generate_signal,
    run_backtest,
    sweep_configs,
)
from src.gridbot.strategy.long_breakout import (
    BreakoutConfig,
    build_breakout_context_with_derivatives,
    generate_breakout_signal,
    run_breakout_backtest,
    sweep_breakout_configs,
)
from src.gridbot.strategy.long_combo import (
    ComboConfig,
    generate_combo_signal,
    run_combo_backtest,
    sweep_combo_configs,
)
from src.gridbot.strategy.long_hybrid import (
    HybridConfig,
    generate_hybrid_signal,
    run_hybrid_backtest,
    sweep_hybrid_configs,
)
from src.gridbot.strategy.long_ntrend import (
    NTrendConfig,
    generate_ntrend_signal,
    run_ntrend_backtest,
    sweep_ntrend_configs,
)
from src.gridbot.strategy.long_orb import (
    OrbConfig,
    build_orb_context_with_derivatives,
    generate_orb_signal,
    run_orb_backtest,
    sweep_orb_configs,
)
from src.gridbot.strategy.portfolio_breakout import (
    PortfolioBreakoutConfig,
    run_portfolio_breakout_backtest,
    sweep_portfolio_breakout_configs,
)
from src.gridbot.strategy.portfolio_orb import (
    PortfolioOrbConfig,
    run_portfolio_orb_backtest,
    sweep_portfolio_orb_configs,
)
from src.gridbot.strategy.portfolio_hybrid import (
    PortfolioHybridConfig,
    run_portfolio_hybrid_backtest,
    sweep_portfolio_hybrid_configs,
)
from src.gridbot.strategy.regime_attribution import (
    attribute_trades_by_regime,
    summarize_regime_attribution,
)
from src.gridbot.strategy.market_state import (
    build_market_state_context,
    classify_market_state,
)
from src.gridbot.strategy.signal_journal import (
    run_orb_signal_journal,
    summarize_allocator_journal,
    summarize_signal_journal,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest or signal ETH long pullback strategy.")
    parser.add_argument("--mode", choices=["signal", "backtest", "sweep", "regime_report", "signal_journal", "market_state"], default="signal")
    parser.add_argument("--preset", choices=["custom", "orb_3pct_v1"], default="custom")
    parser.add_argument("--strategy", choices=["pullback", "breakout", "orb", "hybrid", "ntrend", "combo", "portfolio", "portfolio_orb", "portfolio_hybrid"], default="pullback")
    parser.add_argument("--symbol", default="ETHUSDC")
    parser.add_argument("--symbols", default="ETHUSDC,BTCUSDC,SOLUSDC")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--start-date", help="Backtest start date in YYYY-MM-DD (UTC).")
    parser.add_argument("--end-date", help="Backtest end date in YYYY-MM-DD (UTC, inclusive).")
    parser.add_argument("--equity", type=float, default=200.0)
    parser.add_argument("--compounding", action="store_true", help="Reinvest equity changes during backtests.")
    parser.add_argument("--daily-target-min-pct", type=float, default=3.0)
    parser.add_argument("--daily-target-max-pct", type=float, default=3.0)
    parser.add_argument("--risk", type=float, default=0.5, help="Risk per trade as percent of equity.")
    parser.add_argument("--min-score", type=int, default=55)
    parser.add_argument("--max-leverage", type=float, default=20.0)
    parser.add_argument(
        "--maker-fee",
        type=float,
        default=None,
        help="Maker fee rate. Defaults to 0 for USDC symbols; use 0.0002 for 0.02%.",
    )
    parser.add_argument("--taker-fee", type=float, default=0.0004, help="Taker fee rate; 0.0004 = 0.04%.")
    parser.add_argument("--daily-soft-loss-pct", type=float, default=4.0)
    parser.add_argument("--daily-max-loss-pct", type=float, default=8.0)
    parser.add_argument("--daily-loss-risk-scale", type=float, default=0.55)
    parser.add_argument("--daily-target-stop-pct", type=float, default=3.0)
    parser.add_argument("--keep-trading-after-target", action="store_true")
    parser.add_argument("--max-open-positions", type=int, default=1)
    parser.add_argument("--max-position-margin-pct", type=float, default=35.0)
    parser.add_argument("--cooldown-bars", type=int, default=12)
    parser.add_argument("--loss-cooldown-after", type=int, default=2)
    parser.add_argument("--loss-cooldown-bars", type=int, default=36)
    parser.add_argument("--max-holding-bars", type=int, default=96)
    parser.add_argument("--take-profit-r", default="0.6,1.0,1.5")
    parser.add_argument("--entry-weights", default="0.40,0.35,0.25")
    parser.add_argument("--exit-weights", default="0.40,0.35,0.25")
    parser.add_argument("--breakeven-after-tp", type=int, default=0)
    parser.add_argument("--breakeven-lock-r", type=float, default=0.0)
    parser.add_argument("--disable-accelerator", action="store_true")
    parser.add_argument("--accelerator-min-score", type=int, default=85)
    parser.add_argument("--accelerator-risk", type=float, default=0.35)
    parser.add_argument("--accelerator-margin-pct", type=float, default=8.0)
    parser.add_argument("--accelerator-max-leverage", type=float, default=30.0)
    parser.add_argument("--use-oi-confirmation", action="store_true")
    parser.add_argument("--min-oi-delta-pct", type=float, default=0.5)
    parser.add_argument("--oi-period", default="1h")
    parser.add_argument("--reject-extreme-funding", action="store_true")
    parser.add_argument("--max-funding-rate", type=float, default=0.0003)
    parser.add_argument("--orb-session-start-bar", type=int, default=0)
    parser.add_argument("--orb-opening-range-bars", type=int, default=12)
    parser.add_argument("--orb-min-volume-ratio", type=float, default=0.95)
    parser.add_argument("--orb-stop-atr", type=float, default=1.0)
    parser.add_argument("--portfolio-max-concurrent-positions", type=int, default=2)
    parser.add_argument("--portfolio-margin-cap-pct", type=float, default=70.0)
    parser.add_argument("--portfolio-require-benchmark-trend", action="store_true")
    parser.add_argument("--portfolio-benchmark-risk-scale", type=float, default=0.7)
    parser.add_argument("--portfolio-soft-regime-floor", type=int, default=0)
    parser.add_argument("--portfolio-hard-regime-floor", type=int, default=0)
    parser.add_argument("--portfolio-weak-max-positions", type=int, default=1)
    parser.add_argument("--portfolio-previous-loss-risk-scale", type=float, default=1.0)
    parser.add_argument("--portfolio-previous-loss-max-positions", type=int, default=3)
    parser.add_argument("--portfolio-allow-short", action="store_true")
    parser.add_argument("--portfolio-short-risk-scale", type=float, default=0.75)
    parser.add_argument("--portfolio-short-regime-max-score", type=int, default=2)
    parser.add_argument("--portfolio-allow-reversion", action="store_true")
    parser.add_argument("--portfolio-reversion-risk-scale", type=float, default=0.45)
    parser.add_argument("--portfolio-reversion-regime-max-score", type=int, default=3)
    parser.add_argument("--portfolio-reversion-min-deviation-atr", type=float, default=1.2)
    parser.add_argument("--portfolio-reversion-min-wick-ratio", type=float, default=0.35)
    parser.add_argument("--portfolio-reversion-max-trades-per-day", type=int, default=1)
    parser.add_argument("--portfolio-selector-enabled", action="store_true")
    parser.add_argument("--portfolio-selector-min-score", type=int, default=0)
    parser.add_argument("--portfolio-selector-strong-score", type=int, default=7)
    parser.add_argument("--portfolio-selector-strong-risk-scale", type=float, default=1.0)
    parser.add_argument("--portfolio-selector-min-orb-width-atr", type=float, default=0.35)
    parser.add_argument("--portfolio-selector-max-orb-width-atr", type=float, default=6.0)
    parser.add_argument("--portfolio-rolling-loss-lookback-days", type=int, default=0)
    parser.add_argument("--portfolio-rolling-loss-pause-pct", type=float, default=0.0)
    parser.add_argument("--portfolio-high-conviction-score", type=int, default=88)
    parser.add_argument("--portfolio-high-conviction-weight", type=float, default=1.35)
    parser.add_argument("--portfolio-ai-regime-enabled", action="store_true")
    parser.add_argument("--portfolio-ai-regime-block-enabled", action="store_true")
    parser.add_argument("--portfolio-ai-regime-block-regimes", default="")
    parser.add_argument("--portfolio-ai-regime-min-confidence", type=float, default=0.60)
    parser.add_argument("--portfolio-ai-regime-small-risk-scale", type=float, default=0.45)
    parser.add_argument("--portfolio-ai-regime-aggressive-risk-scale", type=float, default=1.20)
    parser.add_argument("--journal-side", choices=["long", "short", "both", "router"], default="long")
    parser.add_argument("--journal-rolling-loss-lookback-days", type=int, default=0)
    parser.add_argument("--journal-rolling-loss-pause-pct", type=float, default=0.0)
    parser.add_argument("--journal-regime-router", action="store_true", help="Route signal_journal risk by regime/market-state.")
    parser.add_argument("--journal-router-defensive-scale", type=float, default=0.35)
    parser.add_argument("--journal-router-exploratory-scale", type=float, default=0.18)
    parser.add_argument(
        "--journal-short-quality-filter",
        action="store_true",
        help="Block short candidates in fake-risk or high-volatility exhaustion states.",
    )
    parser.add_argument("--journal-throttle-enabled", action="store_true", help="Pause same-day strategy/regime buckets after losses.")
    parser.add_argument("--journal-throttle-strategy-scope", choices=["all", "short", "long"], default="all")
    parser.add_argument("--journal-throttle-max-losses", type=int, default=1)
    parser.add_argument("--journal-throttle-loss-pct", type=float, default=6.0)
    parser.add_argument("--journal-throttle-risk-scale", type=float, default=0.45)
    parser.add_argument("--journal-short-max-holding-bars", type=int, default=0)
    parser.add_argument("--journal-vwap-max-holding-bars", type=int, default=0)
    parser.add_argument("--journal-regime-exit-profile", action="store_true")
    parser.add_argument("--journal-defensive-exit-weights", default="0.55,0.25,0.20")
    parser.add_argument("--journal-defensive-breakeven-after-tp", type=int, default=0)
    parser.add_argument("--journal-defensive-breakeven-lock-r", type=float, default=0.0)
    parser.add_argument("--journal-defensive-max-holding-bars", type=int, default=0)
    parser.add_argument("--journal-defensive-exit-scope", choices=["non_trend", "short_reversion"], default="non_trend")
    parser.add_argument("--journal-regime-allocator", action="store_true")
    parser.add_argument("--journal-allocator-protect-loss-pct", type=float, default=2.0)
    parser.add_argument("--journal-allocator-lock-profit-pct", type=float, default=1.5)
    parser.add_argument("--journal-allocator-protect-scale", type=float, default=0.45)
    parser.add_argument("--journal-allocator-lock-scale", type=float, default=0.65)
    parser.add_argument("--journal-allocator-trend-aggressive-scale", type=float, default=1.15)
    parser.add_argument("--journal-allocator-trend-normal-scale", type=float, default=0.90)
    parser.add_argument("--journal-allocator-trend-normal-low-quality-scale", type=float, default=None)
    parser.add_argument("--journal-allocator-trend-normal-weak-scale", type=float, default=0.45)
    parser.add_argument("--journal-allocator-short-scale", type=float, default=0.85)
    parser.add_argument("--journal-allocator-short-weak-low-atr-scale", type=float, default=None)
    parser.add_argument("--journal-allocator-short-fake-risk-scale", type=float, default=None)
    parser.add_argument("--journal-allocator-short-exhaustion-scale", type=float, default=None)
    parser.add_argument("--journal-allocator-short-exhaustion-strong-scale", type=float, default=None)
    parser.add_argument("--journal-allocator-short-breakdown-scale", type=float, default=1.10)
    parser.add_argument("--journal-allocator-volatility-short-breakdown-scale", type=float, default=0.45)
    parser.add_argument("--journal-allocator-reversion-scale", type=float, default=0.75)
    parser.add_argument("--journal-allocator-weak-pullback-scale", type=float, default=0.45)
    parser.add_argument("--journal-allocator-weak-pullback-normal-scale", type=float, default=None)
    parser.add_argument("--journal-allocator-aggressive-no-trade-scale", type=float, default=0.45)
    parser.add_argument("--journal-allocator-max-risk-pct", type=float, default=12.0)
    parser.add_argument("--journal-allocator-max-margin-pct", type=float, default=35.0)
    parser.add_argument("--market-state-reviewer-enabled", action="store_true")
    parser.add_argument("--market-state-reviewer-mode", choices=["block", "scale"], default="block")
    parser.add_argument("--nim-review", action="store_true", help="Ask NVIDIA NIM to review --mode market_state output.")
    parser.add_argument("--nim-candidate-review", action="store_true", help="Ask NVIDIA NIM to softly scale candidate signal_journal trades.")
    parser.add_argument("--nim-query-policy", choices=["all", "auto"], default="auto")
    parser.add_argument("--nim-cache-path", default="testnet/data/nim_review_cache.json")
    parser.add_argument("--nim-cache-only", action="store_true")
    parser.add_argument("--ai-risk-judge", action="store_true", help="Use bounded AI/local risk judge for high-tail-risk signal_journal trades.")
    parser.add_argument("--ai-risk-query-policy", choices=["local", "auto", "all"], default="local")
    parser.add_argument("--ai-risk-cache-path", default="testnet/data/ai_risk_judge_cache.json")
    parser.add_argument("--ai-risk-cache-only", action="store_true")
    parser.add_argument("--ai-risk-min-confidence", type=float, default=0.60)
    parser.add_argument("--nim-base-url", default=None)
    parser.add_argument("--nim-model", default=None)
    parser.add_argument("--nim-timeout", type=float, default=120.0)
    parser.add_argument("--minimax-fallback", action="store_true")
    parser.add_argument("--minimax-base-url", default=None)
    parser.add_argument("--minimax-model", default=None)
    parser.add_argument("--minimax-timeout", type=float, default=120.0)
    parser.add_argument("--sweep-profile", choices=["balanced", "aggressive", "spec"], default="balanced")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    parser.add_argument("--base-url", default="https://fapi.binance.com")
    raw_args = sys.argv[1:]
    args = parser.parse_args(raw_args)
    _apply_preset(args, raw_args)

    start_ms, end_ms = _resolve_timerange(args.days, args.start_date, args.end_date)
    candles = fetch_klines(args.base_url, args.symbol, args.interval, start_ms, end_ms)
    symbols = tuple(symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip())
    candles_by_symbol = (
        {symbol: fetch_klines(args.base_url, symbol, args.interval, start_ms, end_ms) for symbol in symbols}
        if args.strategy in {"portfolio", "portfolio_orb", "portfolio_hybrid"}
        else None
    )
    config = build_strategy_config_from_args(args)
    breakout_config = BreakoutConfig(
        base=config,
        require_oi_confirmation=args.use_oi_confirmation,
        min_oi_delta_pct=args.min_oi_delta_pct,
        reject_extreme_funding=args.reject_extreme_funding,
        max_funding_rate=args.max_funding_rate,
    )
    breakout_context = _build_breakout_context_from_api(
        candles,
        args.base_url,
        args.symbol,
        args.interval,
        args.oi_period,
        start_ms,
        end_ms,
        breakout_config,
    ) if args.strategy == "breakout" and (args.use_oi_confirmation or args.reject_extreme_funding) else None
    orb_config = OrbConfig(
        base=config,
        session_start_bar=args.orb_session_start_bar,
        opening_range_bars=args.orb_opening_range_bars,
        min_volume_ratio=args.orb_min_volume_ratio,
        stop_atr=args.orb_stop_atr,
        require_oi_confirmation=args.use_oi_confirmation,
        min_oi_delta_pct=args.min_oi_delta_pct,
        reject_extreme_funding=args.reject_extreme_funding,
        max_funding_rate=args.max_funding_rate,
    )
    orb_context = _build_orb_context_from_api(
        candles,
        args.base_url,
        args.symbol,
        args.interval,
        args.oi_period,
        start_ms,
        end_ms,
        orb_config,
    ) if args.strategy == "orb" and (args.use_oi_confirmation or args.reject_extreme_funding) else None
    portfolio_orb_config = (
        build_portfolio_orb_config_from_args(args, config, symbols)
        if args.strategy == "portfolio_orb"
        else None
    )

    if args.mode == "market_state":
        context = build_market_state_context(candles, config)
        decision = classify_market_state(candles, len(candles) - 1, context, config)
        nim_review = _nim_review_payload(decision, args) if args.nim_review and decision is not None else None
        payload = {
            "mode": "market_state",
            "generated_at": _now_iso(),
            "symbol": args.symbol,
            "decision": _market_state_payload(decision),
            "nim_review": nim_review,
        }
        _emit(payload, args.json)
        return

    if args.mode == "signal":
        signal = (
            generate_breakout_signal(candles, breakout_config)
            if args.strategy == "breakout"
            else generate_orb_signal(candles, orb_config)
            if args.strategy == "orb"
            else generate_hybrid_signal(candles, HybridConfig(base=config))
            if args.strategy == "hybrid"
            else generate_ntrend_signal(candles, NTrendConfig(base=config))
            if args.strategy == "ntrend"
            else generate_combo_signal(candles, ComboConfig(base=config))
            if args.strategy == "combo"
            else generate_breakout_signal(
                candles_by_symbol[symbols[0]],
                BreakoutConfig(base=replace(config, symbol=symbols[0])),
            )
            if args.strategy == "portfolio"
            else generate_orb_signal(
                candles_by_symbol[symbols[0]],
                replace(portfolio_orb_config.per_symbol, base=replace(config, symbol=symbols[0])),
            )
            if args.strategy == "portfolio_orb"
            else generate_signal(candles, config)
        )
        payload = {"mode": "signal", "generated_at": _now_iso(), "signal": asdict(signal)}
        _emit(payload, args.json)
        return

    if args.mode == "signal_journal":
        if args.symbol.upper() != "ETHUSDC":
            raise ValueError("signal_journal is currently scoped to --symbol ETHUSDC")
        journal_config = OrbConfig(
            base=config,
            session_start_bar=args.orb_session_start_bar,
            opening_range_bars=args.orb_opening_range_bars,
            min_volume_ratio=args.orb_min_volume_ratio,
            stop_atr=args.orb_stop_atr,
        )
        block_regimes = tuple(
            item.strip()
            for item in args.portfolio_ai_regime_block_regimes.split(",")
            if item.strip()
        ) if args.portfolio_ai_regime_enabled else ()
        summary, rows = run_orb_signal_journal(
            candles,
            journal_config,
            side=args.journal_side,
            block_regimes=block_regimes,
            small_risk_scale=args.portfolio_ai_regime_small_risk_scale if args.portfolio_ai_regime_enabled else 1.0,
            aggressive_risk_scale=args.portfolio_ai_regime_aggressive_risk_scale if args.portfolio_ai_regime_enabled else 1.0,
            market_state_reviewer_enabled=args.market_state_reviewer_enabled,
            market_state_reviewer_mode=args.market_state_reviewer_mode,
            nim_reviewer=_build_cached_nim_reviewer(args) if args.nim_candidate_review else None,
            nim_query_policy=args.nim_query_policy,
            ai_risk_judge=_build_cached_nim_risk_judge(args) if args.ai_risk_judge and args.ai_risk_query_policy in {"auto", "all"} else None,
            ai_risk_judge_enabled=args.ai_risk_judge,
            ai_risk_judge_query_policy=args.ai_risk_query_policy,
            ai_risk_judge_min_confidence=args.ai_risk_min_confidence,
            rolling_loss_lookback_days=args.journal_rolling_loss_lookback_days,
            rolling_loss_pause_pct=args.journal_rolling_loss_pause_pct,
            regime_router_enabled=args.journal_regime_router,
            regime_router_defensive_scale=args.journal_router_defensive_scale,
            regime_router_exploratory_scale=args.journal_router_exploratory_scale,
            short_quality_filter_enabled=args.journal_short_quality_filter,
            journal_throttle_enabled=args.journal_throttle_enabled,
            journal_throttle_strategy_scope=args.journal_throttle_strategy_scope,
            journal_throttle_max_losses=args.journal_throttle_max_losses,
            journal_throttle_loss_pct=args.journal_throttle_loss_pct,
            journal_throttle_risk_scale=args.journal_throttle_risk_scale,
            short_max_holding_bars=args.journal_short_max_holding_bars,
            vwap_max_holding_bars=args.journal_vwap_max_holding_bars,
            regime_exit_profile_enabled=args.journal_regime_exit_profile,
            defensive_exit_weights=_parse_float_tuple(args.journal_defensive_exit_weights, 3, "--journal-defensive-exit-weights"),
            defensive_breakeven_after_tp=args.journal_defensive_breakeven_after_tp,
            defensive_breakeven_lock_r=args.journal_defensive_breakeven_lock_r,
            defensive_max_holding_bars=args.journal_defensive_max_holding_bars,
            defensive_exit_scope=args.journal_defensive_exit_scope,
            regime_allocator_enabled=args.journal_regime_allocator,
            allocator_protect_loss_pct=args.journal_allocator_protect_loss_pct,
            allocator_lock_profit_pct=args.journal_allocator_lock_profit_pct,
            allocator_protect_scale=args.journal_allocator_protect_scale,
            allocator_lock_scale=args.journal_allocator_lock_scale,
            allocator_trend_aggressive_scale=args.journal_allocator_trend_aggressive_scale,
            allocator_trend_normal_scale=args.journal_allocator_trend_normal_scale,
            allocator_trend_normal_low_quality_scale=args.journal_allocator_trend_normal_low_quality_scale,
            allocator_trend_normal_weak_scale=args.journal_allocator_trend_normal_weak_scale,
            allocator_short_scale=args.journal_allocator_short_scale,
            allocator_short_weak_low_atr_scale=args.journal_allocator_short_weak_low_atr_scale,
            allocator_short_fake_risk_scale=args.journal_allocator_short_fake_risk_scale,
            allocator_short_exhaustion_scale=args.journal_allocator_short_exhaustion_scale,
            allocator_short_exhaustion_strong_scale=args.journal_allocator_short_exhaustion_strong_scale,
            allocator_short_breakdown_scale=args.journal_allocator_short_breakdown_scale,
            allocator_volatility_short_breakdown_scale=args.journal_allocator_volatility_short_breakdown_scale,
            allocator_reversion_scale=args.journal_allocator_reversion_scale,
            allocator_weak_pullback_scale=args.journal_allocator_weak_pullback_scale,
            allocator_weak_pullback_normal_scale=args.journal_allocator_weak_pullback_normal_scale,
            allocator_aggressive_no_trade_scale=args.journal_allocator_aggressive_no_trade_scale,
            allocator_max_risk_pct=args.journal_allocator_max_risk_pct,
            allocator_max_margin_pct=args.journal_allocator_max_margin_pct,
        )
        payload = {
            "mode": "signal_journal",
            "generated_at": _now_iso(),
            "summary": _summary_payload(summary),
            "journal_summary": summarize_signal_journal(rows),
            "allocator_summary": summarize_allocator_journal(rows),
            "signals": [row.__dict__ for row in rows],
        }
        _emit(payload, args.json)
        return

    if args.mode in {"backtest", "regime_report"}:
        summary = run_selected_backtest(
            args,
            config,
            candles,
            candles_by_symbol,
            symbols,
            breakout_config,
            breakout_context,
            orb_config,
            orb_context,
            portfolio_orb_config,
        )
        if args.mode == "regime_report":
            if args.strategy != "portfolio_orb":
                raise ValueError("regime_report currently supports --strategy portfolio_orb only")
            benchmark_symbol = portfolio_orb_config.benchmark_symbol if portfolio_orb_config else "BTCUSDC"
            benchmark_candles = candles_by_symbol.get(benchmark_symbol) or candles_by_symbol[symbols[0]]
            rows = attribute_trades_by_regime(summary, benchmark_candles, config)
            payload = {
                "mode": "regime_report",
                "generated_at": _now_iso(),
                "summary": _summary_payload(summary),
                "regime_summary": summarize_regime_attribution(rows),
                "trades": [row.__dict__ for row in rows],
            }
            _emit(payload, args.json)
            return
        payload = {"mode": "backtest", "generated_at": _now_iso(), "summary": _summary_payload(summary)}
        _emit(payload, args.json)
        return

    results = (
        sweep_breakout_configs_with_derivatives(
            candles,
            config,
            profile=args.sweep_profile,
            base_breakout=breakout_config,
            breakout_context=breakout_context,
        )
        if args.strategy == "breakout"
        else sweep_orb_configs_with_derivatives(
            candles,
            config,
            profile=args.sweep_profile,
            base_orb=orb_config,
            orb_context=orb_context,
        )
        if args.strategy == "orb"
        else sweep_hybrid_configs(candles, config, profile=args.sweep_profile)
        if args.strategy == "hybrid"
        else sweep_ntrend_configs(candles, config, profile=args.sweep_profile)
        if args.strategy == "ntrend"
        else sweep_combo_configs(candles, config, profile=args.sweep_profile)
        if args.strategy == "combo"
        else sweep_portfolio_breakout_configs(candles_by_symbol, config)
        if args.strategy == "portfolio"
        else sweep_portfolio_orb_configs(candles_by_symbol, config)
        if args.strategy == "portfolio_orb"
        else sweep_portfolio_hybrid_configs(candles_by_symbol, config)
        if args.strategy == "portfolio_hybrid"
        else sweep_configs(candles, config, profile=args.sweep_profile)
    )[:10]
    payload = {
        "mode": "sweep",
        "generated_at": _now_iso(),
        "top_results": [_summary_payload(result) for result in results],
    }
    _emit(payload, args.json)


def fetch_klines(base_url: str, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
    rows: list[list] = []
    cursor = start_ms

    while cursor < end_ms:
        batch = _get_json_with_retry(
            f"{base_url}/fapi/v1/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1500,
            },
        )
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < 1500:
            break

    return [
        Candle.from_binance_kline(row)
        for row in rows
        if start_ms <= int(row[0]) < end_ms
    ]


def fetch_funding_rates(base_url: str, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    rows: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        batch = _get_json_with_retry(
            f"{base_url}/fapi/v1/fundingRate",
            params={"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000},
        )
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1]["fundingTime"]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < 1000:
            break
    return rows


def fetch_open_interest_hist(base_url: str, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    rows: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        batch = _get_json_with_retry(
            f"{base_url}/futures/data/openInterestHist",
            params={"symbol": symbol, "period": interval, "startTime": cursor, "endTime": end_ms, "limit": 500},
        )
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1]["timestamp"]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < 500:
            break
    return rows


def _get_json_with_retry(url: str, params: dict, max_attempts: int = 4):
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            if attempt == max_attempts - 1:
                break
            time.sleep(0.75 * (2 ** attempt))
    raise last_error if last_error else RuntimeError("request failed")


def _summary_payload(summary) -> dict:
    config = summary.config
    daily_values = list(summary.daily_pnls.values())
    avg_daily_pnl = sum(daily_values) / len(daily_values) if daily_values else 0.0
    stop_atr = summary.params.get("stop_atr", config.stop_atr)
    entry_spacing_atr = summary.params.get("entry_spacing_atr", config.entry_spacing_atr)
    return {
        "symbol": config.symbol,
        "equity_usdc": config.equity_usdc,
        "compounding_enabled": config.compounding_enabled,
        "risk_per_trade_pct": config.risk_per_trade_pct,
        "max_effective_leverage": config.max_effective_leverage,
        "maker_fee_rate": config.maker_fee_rate,
        "taker_fee_rate": config.taker_fee_rate,
        "daily_soft_loss_usdc": round(config.daily_soft_loss_usdc, 4),
        "daily_max_loss_usdc": round(config.daily_max_loss_usdc, 4),
        "daily_loss_risk_scale": config.daily_loss_risk_scale,
        "daily_target_stop_usdc": round(config.daily_target_stop_usdc, 4),
        "stop_trading_after_daily_target": config.stop_trading_after_daily_target,
        "max_open_positions": config.max_open_positions,
        "max_position_margin_pct": config.max_position_margin_pct,
        "cooldown_bars": config.cooldown_bars,
        "loss_cooldown_after": config.max_consecutive_losses_before_cooldown,
        "loss_cooldown_bars": config.consecutive_loss_cooldown_bars,
        "max_holding_bars": config.max_holding_bars,
        "take_profit_r": list(config.take_profit_r),
        "entry_weights": list(config.entry_weights),
        "exit_weights": list(config.exit_weights),
        "accelerator_enabled": config.accelerator_enabled,
        "accelerator_min_score": config.accelerator_min_score,
        "accelerator_risk_per_trade_pct": config.accelerator_risk_per_trade_pct,
        "accelerator_margin_pct": config.accelerator_margin_pct,
        "accelerator_max_effective_leverage": config.accelerator_max_effective_leverage,
        "stop_atr": stop_atr,
        "entry_spacing_atr": entry_spacing_atr,
        "min_score": config.min_score,
        "total_trades": summary.total_trades,
        "net_pnl_usdc": round(summary.net_pnl_usdc, 4),
        "return_pct": round(summary.return_pct, 4),
        "max_drawdown_usdc": round(summary.max_drawdown_usdc, 4),
        "max_drawdown_pct": round(summary.max_drawdown_pct, 4),
        "win_rate_pct": round(summary.win_rate_pct, 2),
        "profit_factor": "inf" if summary.profit_factor == float("inf") else round(summary.profit_factor, 4),
        "expectancy_usdc": round(summary.expectancy_usdc, 4),
        "max_consecutive_losses": summary.max_consecutive_losses,
        "avg_daily_pnl_usdc": round(avg_daily_pnl, 4),
        "avg_daily_return_pct": round(summary.avg_daily_return_pct, 4),
        "best_day_usdc": round(max(daily_values), 4) if daily_values else 0.0,
        "worst_day_usdc": round(min(daily_values), 4) if daily_values else 0.0,
        "daily_target_min_usdc": round(config.daily_target_min_usdc, 4),
        "daily_target_max_usdc": round(config.daily_target_max_usdc, 4),
        "daily_target_4pct_hit_rate_pct": round(summary.daily_target_min_hit_rate_pct, 2),
        "daily_target_5pct_hit_rate_pct": round(summary.daily_target_max_hit_rate_pct, 2),
        "monthly_breakdown": _monthly_breakdown(summary),
        "params": summary.params,
    }


def _market_state_payload(decision) -> dict | None:
    if decision is None:
        return None
    return {
        "trend": decision.trend,
        "ma20_structure": decision.ma20_structure,
        "n_pattern": decision.n_pattern,
        "breakout_quality": decision.breakout_quality,
        "pullback_quality": decision.pullback_quality,
        "volatility": decision.volatility,
        "playbook": decision.playbook,
        "risk_mode": decision.risk_mode,
        "confidence": round(decision.confidence, 4),
        "features": {
            "price": round(decision.features.price, 4),
            "ma20": round(decision.features.ma20, 4) if decision.features.ma20 is not None else None,
            "ma20_slope_atr": round(decision.features.ma20_slope_atr, 4),
            "ema55": round(decision.features.ema55, 4) if decision.features.ema55 is not None else None,
            "vwap": round(decision.features.vwap, 4) if decision.features.vwap is not None else None,
            "atr": round(decision.features.atr, 4),
            "atr_percentile": round(decision.features.atr_percentile, 4),
            "volume_ratio": round(decision.features.volume_ratio, 4),
            "distance_to_ma20_atr": round(decision.features.distance_to_ma20_atr, 4),
            "distance_to_vwap_atr": round(decision.features.distance_to_vwap_atr, 4),
            "close_position_20": round(decision.features.close_position_20, 4),
            "body_to_range": round(decision.features.body_to_range, 4),
        },
        "reasons": list(decision.reasons),
    }


def _nim_review_payload(decision, args) -> dict:
    from src.gridbot.strategy.nim_market_reviewer import NimMarketReviewer

    reviewer = NimMarketReviewer(base_url=args.nim_base_url, model=args.nim_model, timeout=args.nim_timeout)
    review = reviewer.review(decision)
    return {
        "playbook": review.playbook,
        "risk_mode": review.risk_mode,
        "confidence": round(review.confidence, 4),
        "reason_codes": list(review.reason_codes),
    }


def _build_cached_nim_reviewer(args):
    from src.gridbot.strategy.nim_market_reviewer import CachedNimMarketReviewer, NimMarketReviewer

    fallback = (
        NimMarketReviewer(
            base_url=args.minimax_base_url,
            model=args.minimax_model,
            timeout=args.minimax_timeout,
            api_key_env="MINIMAX_API_KEY",
            base_url_env="MINIMAX_BASE_URL",
            model_env="MINIMAX_MODEL",
            default_base_url="https://api.minimax.io/v1",
            default_model="MiniMax-M2.7",
        )
        if args.minimax_fallback
        else None
    )
    return CachedNimMarketReviewer(
        NimMarketReviewer(base_url=args.nim_base_url, model=args.nim_model, timeout=args.nim_timeout),
        args.nim_cache_path,
        cache_only=args.nim_cache_only,
        fallback_reviewer=fallback,
    )


def _build_cached_nim_risk_judge(args):
    from src.gridbot.strategy.nim_market_reviewer import CachedNimRiskJudge, NimRiskJudge

    fallback = (
        NimRiskJudge(
            base_url=args.minimax_base_url,
            model=args.minimax_model,
            timeout=args.minimax_timeout,
            api_key_env="MINIMAX_API_KEY",
            base_url_env="MINIMAX_BASE_URL",
            model_env="MINIMAX_MODEL",
            default_base_url="https://api.minimax.io/v1",
            default_model="MiniMax-M2.7",
        )
        if args.minimax_fallback
        else None
    )
    return CachedNimRiskJudge(
        NimRiskJudge(base_url=args.nim_base_url, model=args.nim_model, timeout=args.nim_timeout),
        args.ai_risk_cache_path,
        cache_only=args.ai_risk_cache_only,
        fallback_reviewer=fallback,
    )


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    mode = payload["mode"]
    print(f"mode={mode} generated_at={payload['generated_at']}")
    if mode == "market_state":
        decision = payload["decision"]
        if decision is None:
            print("decision=None")
            return
        print(
            f"symbol={payload['symbol']} playbook={decision['playbook']} "
            f"risk={decision['risk_mode']} confidence={decision['confidence']}"
        )
        print(
            f"trend={decision['trend']} ma20={decision['ma20_structure']} "
            f"n={decision['n_pattern']} breakout={decision['breakout_quality']} "
            f"pullback={decision['pullback_quality']} vol={decision['volatility']}"
        )
        features = decision["features"]
        print(
            f"price={features['price']} ma20={features['ma20']} ema55={features['ema55']} "
            f"vwap={features['vwap']} atr={features['atr']} atr_pctile={features['atr_percentile']}"
        )
        print(
            f"vol_ratio={features['volume_ratio']} ma20_dist_atr={features['distance_to_ma20_atr']} "
            f"vwap_dist_atr={features['distance_to_vwap_atr']} close_pos20={features['close_position_20']}"
        )
        for reason in decision["reasons"]:
            print(f"reason={reason}")
        nim_review = payload.get("nim_review")
        if nim_review:
            print(
                f"nim_playbook={nim_review['playbook']} nim_risk={nim_review['risk_mode']} "
                f"nim_confidence={nim_review['confidence']}"
            )
            if nim_review["reason_codes"]:
                print("nim_reasons=" + ",".join(nim_review["reason_codes"]))
        return
    if mode == "signal":
        signal = payload["signal"]
        print(f"action={signal['action']} confidence={signal['confidence']} score={signal['score']}")
        print(f"price={signal['price']:.4f} rsi={_fmt(signal['rsi'])} atr={_fmt(signal['atr'])}")
        if signal["entries"]:
            print("entries=" + ", ".join(f"{x:.4f}" for x in signal["entries"]))
            print(f"stop_loss={signal['stop_loss']:.4f}")
            print("take_profits=" + ", ".join(f"{x:.4f}" for x in signal["take_profits"]))
            print(
                f"sizing={signal['sizing_mode']} planned_notional={signal['planned_notional_usdc']:.2f} "
                f"planned_margin={signal['planned_margin_usdc']:.2f} "
                f"leverage_cap={signal['leverage_cap']:.1f} planned_qty={signal['planned_qty']:.6f}"
            )
        print("reasons=" + " | ".join(signal["reasons"]))
        if signal["risk_notes"]:
            print("risk_notes=" + " | ".join(signal["risk_notes"]))
        return

    if mode == "regime_report":
        summary = payload["summary"]
        print(
            f"summary trades={summary['total_trades']} pnl={summary['net_pnl_usdc']} "
            f"return={summary['return_pct']}% dd={summary['max_drawdown_pct']}% "
            f"avg_day_pct={summary['avg_daily_return_pct']}% target_hit={summary['daily_target_4pct_hit_rate_pct']}%"
        )
        print("regime_summary:")
        for row in payload["regime_summary"]:
            print(
                f"  {row['regime']}/{row['risk_mode']} trades={row['trades']} "
                f"pnl={row['net_pnl_usdc']} win={row['win_rate_pct']}% "
                f"pf={row['profit_factor']} avg_r={row['avg_r_multiple']} "
                f"conf={row['avg_confidence']} atr_pctile={row['avg_atr_percentile']} "
                f"vol={row['avg_volume_ratio']}"
            )
        return

    if mode == "signal_journal":
        summary = payload["summary"]
        print(
            f"summary trades={summary['total_trades']} pnl={summary['net_pnl_usdc']} "
            f"return={summary['return_pct']}% dd={summary['max_drawdown_pct']}% "
            f"avg_day_pct={summary['avg_daily_return_pct']}% target_hit={summary['daily_target_4pct_hit_rate_pct']}%"
        )
        print("journal_summary:")
        for row in payload["journal_summary"]:
            print(
                f"  {row['strategy']} {row['regime']}/{row['risk_mode']} playbook={row['market_playbook']} trades={row['trades']} "
                f"nim={row['nim_playbook']}/{row['nim_risk_mode']} "
                f"pnl={row['net_pnl_usdc']} win={row['win_rate_pct']}% "
                f"pf={row['profit_factor']} avg_score={row['avg_score']} "
                f"avg_r={row['avg_r_multiple']} conf={row['avg_regime_confidence']} "
                f"vol={row['avg_volume_ratio']}"
            )
        if payload.get("allocator_summary"):
            print("allocator_summary:")
            for row in payload["allocator_summary"]:
                print(
                    f"  {row['allocator_state']} {row['allocator_profile']} {row['strategy']} {row['regime']} "
                    f"trades={row['trades']} pnl={row['net_pnl_usdc']} "
                    f"margin={row['planned_margin_usdc']} notional={row['planned_notional_usdc']} "
                    f"scale={row['avg_allocator_scale']} risk={row['avg_allocated_risk_pct']} "
                    f"pf={row['profit_factor']}"
                )
        if summary.get("monthly_breakdown"):
            print("monthly:")
            for month in summary["monthly_breakdown"]:
                print(
                    f"  {month['month']} days={month['days']} "
                    f"start={month['start_equity_usdc']} pnl={month['net_pnl_usdc']} "
                    f"ret={month['return_pct']}% avg_day={month['avg_daily_return_pct']}% "
                    f"hit={month['target_hit_rate_pct']}% end={month['end_equity_usdc']}"
                )
        return

    key = "summary" if mode == "backtest" else "top_results"
    rows = [payload[key]] if mode == "backtest" else payload[key]
    for idx, row in enumerate(rows, start=1):
        print(
            f"#{idx} trades={row['total_trades']} pnl={row['net_pnl_usdc']} "
            f"return={row['return_pct']}% dd={row['max_drawdown_pct']}% "
            f"win={row['win_rate_pct']}% pf={row['profit_factor']} "
            f"avg_day={row['avg_daily_pnl_usdc']} avg_day_pct={row['avg_daily_return_pct']}% "
            f"target_hit={row['daily_target_4pct_hit_rate_pct']}% "
            f"risk={row['risk_per_trade_pct']} stop_atr={row['stop_atr']} "
            f"spacing={row['entry_spacing_atr']} score={row['min_score']} "
            f"hold={row['max_holding_bars']} cd={row['cooldown_bars']} "
            f"compound={row['compounding_enabled']} "
            f"fee=m{row['maker_fee_rate']}/t{row['taker_fee_rate']} "
            f"guard=soft{row['daily_soft_loss_usdc']}/hard{row['daily_max_loss_usdc']}/"
            f"target{row['daily_target_stop_usdc']} stop={row['stop_trading_after_daily_target']} "
            f"accel={row['accelerator_enabled']}@{row['accelerator_min_score']}"
        )
        if row.get("params"):
            compact = ",".join(f"{key}={value}" for key, value in row["params"].items())
            print(f"   params={compact}")
        if row.get("monthly_breakdown"):
            print("   monthly:")
            for month in row["monthly_breakdown"]:
                print(
                    "   "
                    f"{month['month']} days={month['days']} "
                    f"start={month['start_equity_usdc']} pnl={month['net_pnl_usdc']} "
                    f"ret={month['return_pct']}% avg_day={month['avg_daily_return_pct']}% "
                    f"hit={month['target_hit_rate_pct']}% end={month['end_equity_usdc']}"
                )


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _default_maker_fee(symbol: str) -> float:
    return 0.0 if symbol.upper().endswith("USDC") else 0.0002


def build_strategy_config_from_args(args: argparse.Namespace) -> StrategyConfig:
    return StrategyConfig(
        symbol=args.symbol,
        equity_usdc=args.equity,
        compounding_enabled=args.compounding,
        daily_target_min_pct=args.daily_target_min_pct,
        daily_target_max_pct=args.daily_target_max_pct,
        risk_per_trade_pct=args.risk,
        min_score=args.min_score,
        max_effective_leverage=args.max_leverage,
        maker_fee_rate=args.maker_fee if args.maker_fee is not None else _default_maker_fee(args.symbol),
        taker_fee_rate=args.taker_fee,
        daily_soft_loss_pct=args.daily_soft_loss_pct,
        daily_max_loss_pct=args.daily_max_loss_pct,
        daily_loss_risk_scale=args.daily_loss_risk_scale,
        daily_target_stop_pct=args.daily_target_stop_pct,
        stop_trading_after_daily_target=not args.keep_trading_after_target,
        max_open_positions=args.max_open_positions,
        max_position_margin_pct=args.max_position_margin_pct,
        cooldown_bars=args.cooldown_bars,
        max_consecutive_losses_before_cooldown=args.loss_cooldown_after,
        consecutive_loss_cooldown_bars=args.loss_cooldown_bars,
        max_holding_bars=args.max_holding_bars,
        take_profit_r=_parse_float_tuple(args.take_profit_r, 3, "--take-profit-r"),
        entry_weights=_parse_float_tuple(args.entry_weights, 3, "--entry-weights"),
        exit_weights=_parse_float_tuple(args.exit_weights, 3, "--exit-weights"),
        breakeven_after_tp=getattr(args, "breakeven_after_tp", 0),
        breakeven_lock_r=getattr(args, "breakeven_lock_r", 0.0),
        accelerator_enabled=not args.disable_accelerator,
        accelerator_min_score=args.accelerator_min_score,
        accelerator_risk_per_trade_pct=args.accelerator_risk,
        accelerator_margin_pct=args.accelerator_margin_pct,
        accelerator_max_effective_leverage=args.accelerator_max_leverage,
    )


def build_portfolio_orb_config_from_args(
    args: argparse.Namespace,
    base: StrategyConfig,
    symbols: tuple[str, ...],
) -> PortfolioOrbConfig:
    return PortfolioOrbConfig(
        symbols=symbols,
        base=base,
        per_symbol=OrbConfig(
            base=base,
            session_start_bar=args.orb_session_start_bar,
            opening_range_bars=args.orb_opening_range_bars,
            min_volume_ratio=args.orb_min_volume_ratio,
            stop_atr=args.orb_stop_atr,
        ),
        max_concurrent_positions=args.portfolio_max_concurrent_positions,
        portfolio_margin_cap_pct=args.portfolio_margin_cap_pct,
        require_benchmark_trend=args.portfolio_require_benchmark_trend,
        benchmark_risk_scale=args.portfolio_benchmark_risk_scale,
        soft_regime_floor=args.portfolio_soft_regime_floor,
        hard_regime_floor=args.portfolio_hard_regime_floor,
        weak_regime_max_positions=args.portfolio_weak_max_positions,
        previous_loss_risk_scale=args.portfolio_previous_loss_risk_scale,
        previous_loss_max_positions=args.portfolio_previous_loss_max_positions,
        allow_short=args.portfolio_allow_short,
        short_risk_scale=args.portfolio_short_risk_scale,
        short_regime_max_score=args.portfolio_short_regime_max_score,
        allow_reversion=args.portfolio_allow_reversion,
        reversion_risk_scale=args.portfolio_reversion_risk_scale,
        reversion_regime_max_score=args.portfolio_reversion_regime_max_score,
        reversion_min_deviation_atr=args.portfolio_reversion_min_deviation_atr,
        reversion_min_wick_ratio=args.portfolio_reversion_min_wick_ratio,
        reversion_max_trades_per_day=args.portfolio_reversion_max_trades_per_day,
        selector_enabled=args.portfolio_selector_enabled,
        selector_min_score=args.portfolio_selector_min_score,
        selector_strong_score=args.portfolio_selector_strong_score,
        selector_strong_risk_scale=args.portfolio_selector_strong_risk_scale,
        selector_min_orb_width_atr=args.portfolio_selector_min_orb_width_atr,
        selector_max_orb_width_atr=args.portfolio_selector_max_orb_width_atr,
        rolling_loss_lookback_days=args.portfolio_rolling_loss_lookback_days,
        rolling_loss_pause_pct=args.portfolio_rolling_loss_pause_pct,
        high_conviction_score=args.portfolio_high_conviction_score,
        high_conviction_weight=args.portfolio_high_conviction_weight,
        ai_regime_enabled=args.portfolio_ai_regime_enabled,
        ai_regime_block_enabled=args.portfolio_ai_regime_block_enabled,
        ai_regime_block_regimes=tuple(
            item.strip()
            for item in args.portfolio_ai_regime_block_regimes.split(",")
            if item.strip()
        ),
        ai_regime_min_confidence=args.portfolio_ai_regime_min_confidence,
        ai_regime_small_risk_scale=args.portfolio_ai_regime_small_risk_scale,
        ai_regime_aggressive_risk_scale=args.portfolio_ai_regime_aggressive_risk_scale,
    )


def run_selected_backtest(
    args: argparse.Namespace,
    config: StrategyConfig,
    candles: list[Candle],
    candles_by_symbol: dict[str, list[Candle]] | None,
    symbols: tuple[str, ...],
    breakout_config: BreakoutConfig,
    breakout_context,
    orb_config: OrbConfig,
    orb_context,
    portfolio_orb_config: PortfolioOrbConfig | None,
):
    return (
        run_breakout_backtest_with_optional_context(candles, breakout_config, breakout_context)
        if args.strategy == "breakout"
        else run_orb_backtest_with_optional_context(candles, orb_config, orb_context)
        if args.strategy == "orb"
        else run_hybrid_backtest(candles, HybridConfig(base=config))
        if args.strategy == "hybrid"
        else run_ntrend_backtest(candles, NTrendConfig(base=config))
        if args.strategy == "ntrend"
        else run_combo_backtest(candles, ComboConfig(base=config))
        if args.strategy == "combo"
        else run_portfolio_breakout_backtest(
            candles_by_symbol,
            PortfolioBreakoutConfig(
                symbols=symbols,
                base=config,
                max_concurrent_positions=args.portfolio_max_concurrent_positions,
                portfolio_margin_cap_pct=args.portfolio_margin_cap_pct,
                require_benchmark_trend=args.portfolio_require_benchmark_trend,
                benchmark_risk_scale=args.portfolio_benchmark_risk_scale,
            ),
        )
        if args.strategy == "portfolio"
        else run_portfolio_orb_backtest(
            candles_by_symbol,
            portfolio_orb_config,
        )
        if args.strategy == "portfolio_orb"
        else run_portfolio_hybrid_backtest(
            candles_by_symbol,
            PortfolioHybridConfig(
                symbols=symbols,
                base=config,
                max_concurrent_positions=args.portfolio_max_concurrent_positions,
                portfolio_margin_cap_pct=args.portfolio_margin_cap_pct,
                require_benchmark_trend=args.portfolio_require_benchmark_trend,
                benchmark_risk_scale=args.portfolio_benchmark_risk_scale,
                high_conviction_score=args.portfolio_high_conviction_score,
                high_conviction_weight=args.portfolio_high_conviction_weight,
            ),
        )
        if args.strategy == "portfolio_hybrid"
        else run_backtest(candles, config)
    )


def _apply_preset(args: argparse.Namespace, raw_args: list[str] | None = None) -> None:
    if args.preset == "custom":
        return
    if args.preset != "orb_3pct_v1":
        raise ValueError(f"Unknown preset: {args.preset}")

    explicit = _explicit_destinations(raw_args or [])
    preset_values = {
        "strategy": "portfolio_orb",
        "compounding": True,
        "daily_target_min_pct": 3.0,
        "daily_target_max_pct": 3.0,
        "risk": 4.2,
        "min_score": 44,
        "max_leverage": 35.0,
        "daily_soft_loss_pct": 4.5,
        "daily_max_loss_pct": 10.0,
        "daily_loss_risk_scale": 0.65,
        "daily_target_stop_pct": 3.0,
        "max_position_margin_pct": 60.0,
        "cooldown_bars": 6,
        "loss_cooldown_after": 3,
        "loss_cooldown_bars": 18,
        "max_holding_bars": 48,
        "take_profit_r": "0.55,1.1,2.2",
        "exit_weights": "0.25,0.35,0.40",
        "portfolio_max_concurrent_positions": 3,
        "portfolio_margin_cap_pct": 100.0,
        "portfolio_benchmark_risk_scale": 0.7,
        "portfolio_soft_regime_floor": 3,
        "portfolio_hard_regime_floor": 0,
        "portfolio_weak_max_positions": 1,
        "portfolio_previous_loss_risk_scale": 0.55,
        "portfolio_previous_loss_max_positions": 1,
        "portfolio_allow_short": False,
        "portfolio_allow_reversion": False,
        "portfolio_selector_enabled": False,
        "portfolio_high_conviction_score": 88,
        "portfolio_high_conviction_weight": 1.6,
        "portfolio_ai_regime_enabled": False,
        "portfolio_ai_regime_block_enabled": False,
        "portfolio_ai_regime_block_regimes": "",
        "portfolio_ai_regime_min_confidence": 0.60,
        "portfolio_ai_regime_small_risk_scale": 0.45,
        "portfolio_ai_regime_aggressive_risk_scale": 1.20,
        "portfolio_rolling_loss_lookback_days": 2,
        "portfolio_rolling_loss_pause_pct": 8.0,
        "orb_session_start_bar": 0,
        "orb_opening_range_bars": 9,
        "orb_min_volume_ratio": 0.8,
        "orb_stop_atr": 0.6,
    }
    for dest, value in preset_values.items():
        if dest not in explicit:
            setattr(args, dest, value)


def _explicit_destinations(raw_args: list[str]) -> set[str]:
    destinations: set[str] = set()
    for token in raw_args:
        if not token.startswith("--"):
            continue
        flag = token.split("=", 1)[0]
        destinations.add(flag[2:].replace("-", "_"))
    return destinations


def _parse_float_tuple(value: str, expected_len: int, flag_name: str) -> tuple[float, ...]:
    try:
        parsed = tuple(float(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise ValueError(f"{flag_name} must be comma-separated numbers") from exc
    if len(parsed) != expected_len:
        raise ValueError(f"{flag_name} must contain exactly {expected_len} numbers")
    if flag_name.endswith("weights") and abs(sum(parsed) - 1.0) > 1e-6:
        raise ValueError(f"{flag_name} must sum to 1.0")
    return parsed


def _derivatives_proxy_symbol(symbol: str) -> str:
    upper = symbol.upper()
    if upper.endswith("USDC"):
        return f"{upper[:-4]}USDT"
    return upper


def run_breakout_backtest_with_optional_context(candles, config, context):
    if context is None:
        return run_breakout_backtest(candles, config)
    from src.gridbot.strategy.long_breakout import run_breakout_backtest_with_context
    return run_breakout_backtest_with_context(candles, config, context)


def sweep_breakout_configs_with_derivatives(
    candles,
    base_config,
    profile,
    base_breakout,
    breakout_context,
):
    if breakout_context is None:
        return sweep_breakout_configs(candles, base_config, profile=profile)
    from src.gridbot.strategy.long_breakout import sweep_breakout_configs_with_context
    return sweep_breakout_configs_with_context(candles, base_config, breakout_context, profile=profile, template=base_breakout)


def run_orb_backtest_with_optional_context(candles, config, context):
    if context is None:
        return run_orb_backtest(candles, config)
    from src.gridbot.strategy.long_orb import run_orb_backtest_with_context
    return run_orb_backtest_with_context(candles, config, context)


def sweep_orb_configs_with_derivatives(
    candles,
    base_config,
    profile,
    base_orb,
    orb_context,
):
    return sweep_orb_configs(candles, base_config, profile=profile, template=base_orb, context=orb_context)


def _build_breakout_context_from_api(candles, base_url, symbol, interval, oi_period, start_ms, end_ms, config):
    derivative_symbol = _derivatives_proxy_symbol(symbol)
    oi_values = None
    funding_values = None
    if config.require_oi_confirmation:
        oi_start_ms = max(start_ms, end_ms - 20 * 24 * 3600 * 1000)
        oi_rows = fetch_open_interest_hist(base_url, derivative_symbol, oi_period, oi_start_ms, end_ms)
        oi_values = _align_oi_delta_series(oi_rows)
    if config.reject_extreme_funding:
        funding_rows = fetch_funding_rates(base_url, derivative_symbol, start_ms, end_ms)
        funding_values = _align_step_series(
            [(int(row["fundingTime"]), float(row["fundingRate"])) for row in funding_rows]
        )
    return build_breakout_context_with_derivatives(
        candles,
        config,
        oi_delta_pct_values=_project_series_to_candles(candles, oi_values),
        funding_rate_values=_project_series_to_candles(candles, funding_values),
    )


def _build_orb_context_from_api(candles, base_url, symbol, interval, oi_period, start_ms, end_ms, config):
    derivative_symbol = _derivatives_proxy_symbol(symbol)
    oi_values = None
    funding_values = None
    if config.require_oi_confirmation:
        oi_start_ms = max(start_ms, end_ms - 20 * 24 * 3600 * 1000)
        oi_rows = fetch_open_interest_hist(base_url, derivative_symbol, oi_period, oi_start_ms, end_ms)
        oi_values = _align_oi_delta_series(oi_rows)
    if config.reject_extreme_funding:
        funding_rows = fetch_funding_rates(base_url, derivative_symbol, start_ms, end_ms)
        funding_values = _align_step_series(
            [(int(row["fundingTime"]), float(row["fundingRate"])) for row in funding_rows]
        )
    return build_orb_context_with_derivatives(
        candles,
        config,
        oi_delta_pct_values=_project_series_to_candles(candles, oi_values),
        funding_rate_values=_project_series_to_candles(candles, funding_values),
    )


def _align_oi_delta_series(rows: list[dict]) -> list[tuple[int, float]]:
    aligned: list[tuple[int, float]] = []
    previous = None
    for row in sorted(rows, key=lambda item: int(item["timestamp"])):
        current = float(row.get("sumOpenInterestValue") or row.get("sumOpenInterest") or 0.0)
        timestamp = int(row["timestamp"])
        if previous and previous > 0:
            aligned.append((timestamp, (current - previous) / previous * 100))
        else:
            aligned.append((timestamp, 0.0))
        previous = current
    return aligned


def _align_step_series(points: list[tuple[int, float]]) -> list[tuple[int, float]]:
    return sorted(points, key=lambda item: item[0])


def _project_series_to_candles(candles: list[Candle], series: list[tuple[int, float]] | None) -> list[float | None] | None:
    if not series:
        return None
    values: list[float | None] = []
    series_index = 0
    current_value: float | None = None
    for candle in candles:
        while series_index < len(series) and series[series_index][0] <= candle.open_time_ms:
            current_value = series[series_index][1]
            series_index += 1
        values.append(current_value)
    return values


def _resolve_timerange(days: int, start_date: str | None, end_date: str | None) -> tuple[int, int]:
    if not start_date and not end_date:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - days * 24 * 3600 * 1000
        return start_ms, end_ms

    if not start_date or not end_date:
        raise ValueError("--start-date and --end-date must be provided together.")

    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    if end_dt <= start_dt:
        raise ValueError("--end-date must be on or after --start-date.")
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)


def _monthly_breakdown(summary) -> list[dict]:
    config = summary.config
    equity = config.equity_usdc
    months: dict[str, dict[str, float | int | None]] = {}

    for day, pnl in sorted(summary.daily_pnls.items()):
        month_key = day[:7]
        bucket = months.setdefault(
            month_key,
            {
                "days": 0,
                "start_equity": None,
                "end_equity": None,
                "net_pnl": 0.0,
                "target_hits": 0,
                "total_return_pct": 0.0,
            },
        )
        if bucket["start_equity"] is None:
            bucket["start_equity"] = equity

        start_equity = equity if config.compounding_enabled else config.equity_usdc
        day_return_pct = (pnl / start_equity * 100) if start_equity else 0.0
        target_usdc = start_equity * config.daily_target_min_pct / 100 if start_equity else 0.0
        if pnl >= target_usdc:
            bucket["target_hits"] += 1

        bucket["days"] += 1
        bucket["net_pnl"] += pnl
        bucket["total_return_pct"] += day_return_pct
        equity += pnl
        bucket["end_equity"] = equity

    rows: list[dict] = []
    for month_key, bucket in months.items():
        days = int(bucket["days"])
        rows.append(
            {
                "month": month_key,
                "days": days,
                "start_equity_usdc": round(float(bucket["start_equity"] or 0.0), 4),
                "net_pnl_usdc": round(float(bucket["net_pnl"]), 4),
                "return_pct": round(
                    (float(bucket["net_pnl"]) / float(bucket["start_equity"]) * 100)
                    if bucket["start_equity"]
                    else 0.0,
                    4,
                ),
                "avg_daily_return_pct": round((float(bucket["total_return_pct"]) / days) if days else 0.0, 4),
                "target_hit_rate_pct": round((int(bucket["target_hits"]) / days * 100) if days else 0.0, 2),
                "end_equity_usdc": round(float(bucket["end_equity"] or 0.0), 4),
            }
        )
    return rows


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


if __name__ == "__main__":
    main()
