"""Trade-level signal journal for single-symbol strategy review."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from src.gridbot.strategy.long_breakout import _simulate_breakout
from src.gridbot.strategy.long_orb import (
    OrbConfig,
    build_orb_context,
    generate_orb_short_signal_at,
    generate_orb_signal_at,
    generate_vwap_reversion_long_signal_at,
    generate_vwap_reversion_short_signal_at,
    simulate_orb_short,
)
from src.gridbot.strategy.long_pullback import (
    BacktestSummary,
    Candle,
    SignalPlan,
    StrategyConfig,
    TradeResult,
    _daily_guard_reason,
    _day_key,
    _drawdown_pct,
    _empty_daily_pnls,
    _risk_adjusted_config,
    _summary,
)
from src.gridbot.strategy.long_orb import _to_breakout_proxy
from src.gridbot.strategy.market_state import build_market_state_context, classify_market_state
from src.gridbot.strategy.regime import build_regime_context, classify_regime


@dataclass(frozen=True)
class SignalJournalRow:
    symbol: str
    strategy: str
    signal_time_ms: int
    signal_time_iso: str
    entry_time_ms: int
    score: int
    confidence: int
    planned_margin_usdc: float
    planned_notional_usdc: float
    leverage_cap: float
    allocator_state: str
    allocator_profile: str
    allocator_scale: float
    allocated_risk_pct: float
    allocated_margin_pct: float
    regime: str
    risk_mode: str
    regime_confidence: float
    market_playbook: str
    market_risk_mode: str
    market_confidence: float
    market_trend: str
    market_ma20_structure: str
    market_n_pattern: str
    market_breakout_quality: str
    market_pullback_quality: str
    nim_playbook: str
    nim_risk_mode: str
    nim_confidence: float
    ai_risk_decision: str
    ai_risk_level: str
    ai_risk_scale: float
    ai_risk_confidence: float
    ai_risk_reason_codes: str
    atr_percentile: float
    volume_ratio: float
    trend_slope_atr: float
    close_position_lookback: float
    pnl_usdc: float
    r_multiple: float
    exit_reason: str
    hold_bars: int


@dataclass(frozen=True)
class LocalNimReview:
    playbook: str
    risk_mode: str
    confidence: float
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalAiRiskReview:
    decision: str
    risk_level: str
    risk_scale: float
    confidence: float
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class LiveRouterDecision:
    signal: SignalPlan
    strategy: str
    regime: str
    risk_mode: str
    market_playbook: str
    allocator_state: str
    allocator_profile: str
    allocator_scale: float
    max_holding_bars: int


def _router_block(reason: str, **details: object) -> str:
    if not details:
        return reason
    rendered = ", ".join(f"{key}={value}" for key, value in details.items())
    return f"{reason} ({rendered})"


def _generate_router_allocator_live_decision_debug(
    candles: list[Candle],
    base: StrategyConfig,
    day_pnl: float = 0.0,
    nim_hard_block_enabled: bool = True,
) -> tuple[LiveRouterDecision | None, str]:
    """Return the live router decision plus a precise block reason when absent."""

    if not candles:
        return None, "no_candles"

    config = OrbConfig(
        base=base,
        session_start_bar=0,
        opening_range_bars=9,
        min_volume_ratio=0.8,
        stop_atr=0.6,
    )
    context = build_orb_context(candles, config)
    regime_context = build_regime_context(candles, config.base)
    market_context = build_market_state_context(candles, config.base)

    index = len(candles) - 1
    equity_base = _equity_base(base, base.equity_usdc)
    daily_guard = _daily_guard_reason(equity_base, day_pnl)
    if daily_guard:
        return None, _router_block("daily_guard", reason=daily_guard, day_pnl=round(day_pnl, 4))

    runtime_base = _risk_adjusted_config(equity_base, day_pnl)
    decision = classify_regime(candles, index, regime_context, runtime_base)
    runtime_config = config if runtime_base is base else _replace_base(config, runtime_base)
    market_decision = classify_market_state(candles, index, market_context, runtime_base)

    signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, "router")
    expected_action = _expected_action(strategy)
    if signal.action != expected_action:
        return None, _router_block(
            "initial_signal_mismatch",
            strategy=strategy,
            signal_action=signal.action,
            expected_action=expected_action,
            score=signal.score,
            regime=getattr(decision, "regime", "unknown"),
        )

    routed_base = _regime_router_adjusted_base(
        runtime_base,
        strategy,
        decision,
        market_decision,
        0.70,
        0.35,
    )
    if routed_base is None:
        return None, _router_block(
            "regime_router_blocked",
            strategy=strategy,
            regime=getattr(decision, "regime", "unknown"),
            risk_mode=getattr(decision, "risk_mode", "unknown"),
            market_playbook=getattr(market_decision, "playbook", "unknown"),
            market_risk_mode=getattr(market_decision, "risk_mode", "unknown"),
        )
    if routed_base is not runtime_base:
        runtime_base = routed_base
        runtime_config = _replace_base(runtime_config, runtime_base)
        signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, "router")
        expected_action = _expected_action(strategy)
        if signal.action != expected_action:
            return None, _router_block(
                "post_router_signal_mismatch",
                strategy=strategy,
                signal_action=signal.action,
                expected_action=expected_action,
                score=signal.score,
                regime=getattr(decision, "regime", "unknown"),
            )

    nim_review = _local_nim_policy_review("auto", strategy, signal, market_decision)
    if nim_review is not None:
        if _nim_review_rejected_by_market_state(strategy, decision, market_decision, nim_review):
            return None, _router_block(
                "nim_rejected_by_market_state",
                strategy=strategy,
                nim_playbook=nim_review.playbook,
                nim_risk_mode=nim_review.risk_mode,
                market_playbook=getattr(market_decision, "playbook", "unknown"),
                regime=getattr(decision, "regime", "unknown"),
            )
        scaled_base = _nim_scaled_base(runtime_base, nim_review, hard_block_enabled=nim_hard_block_enabled)
        if scaled_base is None:
            return None, _router_block(
                "nim_scaled_to_zero",
                strategy=strategy,
                nim_playbook=nim_review.playbook,
                nim_risk_mode=nim_review.risk_mode,
                nim_confidence=round(nim_review.confidence, 3),
            )
        if scaled_base is not runtime_base:
            runtime_base = scaled_base
            runtime_config = _replace_base(runtime_config, runtime_base)
            signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, "router")
            expected_action = _expected_action(strategy)
            if signal.action != expected_action:
                return None, _router_block(
                    "post_nim_signal_mismatch",
                    strategy=strategy,
                    signal_action=signal.action,
                    expected_action=expected_action,
                    score=signal.score,
                    nim_playbook=nim_review.playbook,
                    nim_risk_mode=nim_review.risk_mode,
                )

    allocated_base, allocation = _regime_allocator_adjusted_base(
        runtime_base,
        strategy,
        decision,
        market_decision,
        nim_review,
        day_pnl,
        2.0,
        1.5,
        0.45,
        1.00,
        3.50,
        1.00,
        0.35,
        0.35,
        0.55,
        0.25,
        0.05,
        0.30,
        None,
        1.25,
        0.45,
        0.05,
        0.30,
        0.20,
        0.05,
        0.0,
        100.0,
        signal_score=signal.score,
    )
    if allocated_base is None:
        return None, _router_block(
            "allocator_blocked",
            strategy=strategy,
            regime=getattr(decision, "regime", "unknown"),
            market_playbook=getattr(market_decision, "playbook", "unknown"),
            nim_playbook=getattr(nim_review, "playbook", "none"),
            nim_risk_mode=getattr(nim_review, "risk_mode", "none"),
            score=signal.score,
        )
    allocator_state = allocation["state"]
    allocator_profile = allocation["profile"]
    allocator_scale = allocation["scale"]

    if allocated_base is not runtime_base:
        runtime_base = allocated_base
        runtime_config = _replace_base(runtime_config, runtime_base)
        signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, "router")
        expected_action = _expected_action(strategy)
        if signal.action != expected_action:
            return None, _router_block(
                "post_allocator_signal_mismatch",
                strategy=strategy,
                signal_action=signal.action,
                expected_action=expected_action,
                score=signal.score,
                allocator_state=allocator_state,
                allocator_profile=allocator_profile,
                allocator_scale=allocator_scale,
            )

    ai_risk_review = _local_ai_risk_review(
        strategy,
        decision,
        market_decision,
        signal,
        allocator_state,
        allocator_profile,
        runtime_base,
    )
    if ai_risk_review is not None:
        if ai_risk_review.decision == "reject" or ai_risk_review.risk_scale <= 0:
            return None, _router_block(
                "ai_risk_rejected",
                strategy=strategy,
                ai_decision=ai_risk_review.decision,
                ai_level=ai_risk_review.risk_level,
                ai_scale=ai_risk_review.risk_scale,
                reason_codes="|".join(ai_risk_review.reason_codes) or "none",
            )
        if ai_risk_review.decision == "reduce" and ai_risk_review.risk_scale < 1.0:
            runtime_base = _scaled_base(runtime_base, ai_risk_review.risk_scale)
            runtime_config = _replace_base(runtime_config, runtime_base)
            signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, "router")
            expected_action = _expected_action(strategy)
            if signal.action != expected_action:
                return None, _router_block(
                    "post_ai_risk_signal_mismatch",
                    strategy=strategy,
                    signal_action=signal.action,
                    expected_action=expected_action,
                    score=signal.score,
                    ai_scale=ai_risk_review.risk_scale,
                    ai_level=ai_risk_review.risk_level,
                )

    trade_config = _strategy_trade_config(
        runtime_config,
        strategy,
        decision,
        0,
        0,
        True,
        (0.25, 0.35, 0.40),
        0,
        0.0,
        24,
        "short_reversion",
    )
    return (
        LiveRouterDecision(
            signal=signal,
            strategy=strategy,
            regime=decision.regime if decision is not None else "unknown",
            risk_mode=decision.risk_mode if decision is not None else "unknown",
            market_playbook=market_decision.playbook if market_decision is not None else "unknown",
            allocator_state=allocator_state,
            allocator_profile=allocator_profile,
            allocator_scale=allocator_scale,
            max_holding_bars=trade_config.base.max_holding_bars,
        ),
        "ok",
    )


def _generate_router_allocator_live_decision(
    candles: list[Candle],
    base: StrategyConfig,
    day_pnl: float = 0.0,
    nim_hard_block_enabled: bool = True,
) -> LiveRouterDecision | None:
    """Generate the current live decision for the high-return router family."""

    decision, _ = _generate_router_allocator_live_decision_debug(
        candles,
        base,
        day_pnl,
        nim_hard_block_enabled=nim_hard_block_enabled,
    )
    return decision


def generate_router_allocator_high_return_live_decision(
    candles: list[Candle],
    base: StrategyConfig,
    day_pnl: float = 0.0,
) -> LiveRouterDecision | None:
    """Live wrapper for the saved high-return router allocator family."""

    return _generate_router_allocator_live_decision(
        candles,
        base,
        day_pnl,
        nim_hard_block_enabled=False,
    )


def explain_router_allocator_high_return_live_block(
    candles: list[Candle],
    base: StrategyConfig,
    day_pnl: float = 0.0,
) -> str:
    """Explain why the high-return live router path returned no tradable decision."""

    _, reason = _generate_router_allocator_live_decision_debug(
        candles,
        base,
        day_pnl,
        nim_hard_block_enabled=False,
    )
    return reason


def generate_router_allocator_v13_trend350_live_decision(
    candles: list[Candle],
    base: StrategyConfig,
    day_pnl: float = 0.0,
) -> LiveRouterDecision | None:
    """Backward-compatible alias for the legacy trend350 live label."""

    return _generate_router_allocator_live_decision(
        candles,
        base,
        day_pnl,
        nim_hard_block_enabled=True,
    )


def explain_router_allocator_v13_trend350_live_block(
    candles: list[Candle],
    base: StrategyConfig,
    day_pnl: float = 0.0,
) -> str:
    """Backward-compatible block reason helper for the legacy trend350 live label."""

    _, reason = _generate_router_allocator_live_decision_debug(
        candles,
        base,
        day_pnl,
        nim_hard_block_enabled=True,
    )
    return reason


def run_orb_signal_journal(
    candles: list[Candle],
    config: OrbConfig | None = None,
    side: Literal["long", "short", "both", "router"] = "long",
    block_regimes: tuple[str, ...] = (),
    small_risk_scale: float = 1.0,
    aggressive_risk_scale: float = 1.0,
    market_state_reviewer_enabled: bool = False,
    market_state_reviewer_mode: Literal["block", "scale"] = "block",
    nim_reviewer=None,
    nim_query_policy: Literal["all", "auto"] = "all",
    ai_risk_judge=None,
    ai_risk_judge_enabled: bool = False,
    ai_risk_judge_query_policy: Literal["local", "auto", "all"] = "local",
    ai_risk_judge_min_confidence: float = 0.60,
    rolling_loss_lookback_days: int = 0,
    rolling_loss_pause_pct: float = 0.0,
    regime_router_enabled: bool = False,
    regime_router_defensive_scale: float = 0.35,
    regime_router_exploratory_scale: float = 0.18,
    short_quality_filter_enabled: bool = False,
    journal_throttle_enabled: bool = False,
    journal_throttle_strategy_scope: Literal["all", "short", "long"] = "all",
    journal_throttle_max_losses: int = 1,
    journal_throttle_loss_pct: float = 6.0,
    journal_throttle_risk_scale: float = 0.45,
    short_max_holding_bars: int = 0,
    vwap_max_holding_bars: int = 0,
    regime_exit_profile_enabled: bool = False,
    defensive_exit_weights: tuple[float, float, float] = (0.55, 0.25, 0.20),
    defensive_breakeven_after_tp: int = 0,
    defensive_breakeven_lock_r: float = 0.0,
    defensive_max_holding_bars: int = 0,
    defensive_exit_scope: Literal["non_trend", "short_reversion"] = "non_trend",
    regime_allocator_enabled: bool = False,
    allocator_protect_loss_pct: float = 2.0,
    allocator_lock_profit_pct: float = 1.5,
    allocator_protect_scale: float = 0.45,
    allocator_lock_scale: float = 0.65,
    allocator_trend_aggressive_scale: float = 1.15,
    allocator_trend_normal_scale: float = 0.85,
    allocator_trend_normal_low_quality_scale: float | None = None,
    allocator_trend_normal_weak_scale: float = 0.45,
    allocator_short_scale: float = 0.55,
    allocator_short_weak_low_atr_scale: float | None = None,
    allocator_short_fake_risk_scale: float | None = None,
    allocator_short_exhaustion_scale: float | None = None,
    allocator_short_exhaustion_strong_scale: float | None = None,
    allocator_short_breakdown_scale: float = 1.10,
    allocator_volatility_short_breakdown_scale: float = 0.45,
    allocator_reversion_scale: float = 0.35,
    allocator_weak_pullback_scale: float = 0.45,
    allocator_weak_pullback_normal_scale: float | None = None,
    allocator_aggressive_no_trade_scale: float = 0.45,
    allocator_max_risk_pct: float = 12.0,
    allocator_max_margin_pct: float = 35.0,
) -> tuple[BacktestSummary, list[SignalJournalRow]]:
    config = config or OrbConfig()
    context = build_orb_context(candles, config)
    regime_context = build_regime_context(candles, config.base)
    market_context = (
        build_market_state_context(candles, config.base)
        if market_state_reviewer_enabled or regime_router_enabled or short_quality_filter_enabled or ai_risk_judge_enabled
        else None
    )
    base = config.base
    warmup = max(config.volume_lookback, base.ema_slow_period, base.vwap_period, config.opening_range_bars) + 2
    trades: list[TradeResult] = []
    rows: list[SignalJournalRow] = []
    equity = base.equity_usdc
    peak_equity = equity
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    daily = _empty_daily_pnls(candles)
    consecutive_losses = 0
    cooldown = 0
    throttle_state: dict[tuple[str, tuple[str, ...]], dict[str, float | int]] = {}
    index = warmup

    while index < len(candles) - 2:
        if base.max_open_positions < 1:
            break
        if cooldown > 0:
            cooldown -= 1
            index += 1
            continue

        equity_base = _equity_base(base, equity)
        day = _day_key(candles[index].open_time_ms)
        day_pnl = daily.get(day, 0.0)
        if _daily_guard_reason(equity_base, day_pnl):
            index += 1
            continue
        if _rolling_loss_guard(equity_base, day, daily, rolling_loss_lookback_days, rolling_loss_pause_pct):
            index += 1
            continue

        runtime_base = _risk_adjusted_config(equity_base, day_pnl)
        decision = classify_regime(candles, index, regime_context, runtime_base)
        if decision is not None:
            if decision.regime in block_regimes:
                index += 1
                continue
            runtime_base = _reviewer_adjusted_base(runtime_base, decision.risk_mode, small_risk_scale, aggressive_risk_scale)
        runtime_config = config if runtime_base is base else _replace_base(config, runtime_base)
        market_decision = (
            classify_market_state(candles, index, market_context, runtime_base)
            if market_state_reviewer_enabled
            else None
        )
        signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, side)
        expected_action = _expected_action(strategy)
        if signal.action != expected_action:
            index += 1
            continue
        if regime_router_enabled:
            if market_decision is None:
                market_decision = classify_market_state(candles, index, market_context, runtime_base)
            routed_base = _regime_router_adjusted_base(
                runtime_base,
                strategy,
                decision,
                market_decision,
                regime_router_defensive_scale,
                regime_router_exploratory_scale,
            )
            if routed_base is None:
                index += 1
                continue
            if routed_base is not runtime_base:
                runtime_base = routed_base
                runtime_config = _replace_base(runtime_config, runtime_base)
                signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, side)
                expected_action = _expected_action(strategy)
                if signal.action != expected_action:
                    index += 1
                    continue
        if short_quality_filter_enabled and strategy == "orb_short":
            if market_decision is None:
                market_decision = classify_market_state(candles, index, market_context, runtime_base)
            if market_decision is None or _short_quality_filter_blocks(strategy, decision, market_decision):
                index += 1
                continue
        if (
            market_state_reviewer_enabled
            and market_state_reviewer_mode == "block"
            and not _market_state_allows(strategy, market_decision)
        ):
            index += 1
            continue
        if market_state_reviewer_enabled and market_state_reviewer_mode == "scale":
            scaled_base = _market_state_scaled_base(runtime_base, strategy, market_decision)
            if scaled_base is None:
                index += 1
                continue
            if scaled_base is not runtime_base:
                runtime_base = scaled_base
                runtime_config = _replace_base(runtime_config, runtime_base)
                signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, side)
                expected_action = _expected_action(strategy)
                if signal.action != expected_action:
                    index += 1
                    continue
        nim_review = None
        if nim_reviewer is not None:
            if market_decision is None:
                market_decision = classify_market_state(candles, index, market_context, runtime_base)
            if market_decision is None:
                index += 1
                continue
            nim_review = _local_nim_policy_review(nim_query_policy, strategy, signal, market_decision)
            if nim_review is None:
                nim_review = nim_reviewer.review(
                    market_decision,
                    _nim_cache_key(base.symbol, candles[index], strategy, signal),
                    candidate=_nim_candidate_payload(strategy, signal),
                )
            if _nim_review_rejected_by_market_state(strategy, decision, market_decision, nim_review):
                index += 1
                continue
            scaled_base = _nim_scaled_base(runtime_base, nim_review)
            if scaled_base is None:
                index += 1
                continue
            if scaled_base is not runtime_base:
                runtime_base = scaled_base
                runtime_config = _replace_base(runtime_config, runtime_base)
                signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, side)
                expected_action = _expected_action(strategy)
                if signal.action != expected_action:
                    index += 1
                    continue

        allocator_state = "not_used"
        allocator_profile = "base"
        allocator_scale = 1.0
        ai_risk_review = None
        if regime_allocator_enabled:
            allocated_base, allocation = _regime_allocator_adjusted_base(
                runtime_base,
                strategy,
                decision,
                market_decision,
                nim_review,
                day_pnl,
                allocator_protect_loss_pct,
                allocator_lock_profit_pct,
                allocator_protect_scale,
                allocator_lock_scale,
                allocator_trend_aggressive_scale,
                allocator_trend_normal_scale,
                allocator_trend_normal_low_quality_scale,
                allocator_trend_normal_weak_scale,
                allocator_short_scale,
                allocator_short_weak_low_atr_scale,
                allocator_short_fake_risk_scale,
                allocator_short_exhaustion_scale,
                allocator_short_exhaustion_strong_scale,
                allocator_short_breakdown_scale,
                allocator_volatility_short_breakdown_scale,
                allocator_reversion_scale,
                allocator_weak_pullback_scale,
                allocator_weak_pullback_normal_scale,
                allocator_aggressive_no_trade_scale,
                allocator_max_risk_pct,
                allocator_max_margin_pct,
                signal_score=signal.score,
            )
            allocator_state = allocation["state"]
            allocator_profile = allocation["profile"]
            allocator_scale = allocation["scale"]
            if allocated_base is None:
                index += 1
                continue
            if allocated_base is not runtime_base:
                runtime_base = allocated_base
                runtime_config = _replace_base(runtime_config, runtime_base)
                signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, side)
                expected_action = _expected_action(strategy)
                if signal.action != expected_action:
                    index += 1
                    continue

        if ai_risk_judge_enabled:
            if market_decision is None:
                market_decision = classify_market_state(candles, index, market_context, runtime_base)
            ai_risk_review = _local_ai_risk_review(
                strategy,
                decision,
                market_decision,
                signal,
                allocator_state,
                allocator_profile,
                runtime_base,
            )
            should_query_ai = (
                ai_risk_judge is not None
                and market_decision is not None
                and ai_risk_judge_query_policy in {"all", "auto"}
                and (ai_risk_review is None or ai_risk_judge_query_policy == "all")
                and _ai_risk_judge_ai_query_needed(
                    strategy,
                    market_decision,
                    allocator_state,
                    allocator_profile,
                    runtime_base,
                )
            )
            if should_query_ai:
                ai_risk_review = ai_risk_judge.review(
                    market_decision,
                    _ai_risk_cache_key(base.symbol, candles[index], strategy, signal, allocator_state, allocator_profile),
                    candidate=_ai_risk_candidate_payload(
                        strategy,
                        signal,
                        allocator_state,
                        allocator_profile,
                        allocator_scale,
                        runtime_base,
                        day_pnl,
                    ),
                )
                ai_risk_review = _bounded_external_ai_risk_review(ai_risk_review)
            if ai_risk_review is not None and ai_risk_review.confidence >= ai_risk_judge_min_confidence:
                if ai_risk_review.decision == "reject" or ai_risk_review.risk_scale <= 0:
                    index += 1
                    continue
                if ai_risk_review.decision == "reduce" and ai_risk_review.risk_scale < 1.0:
                    runtime_base = _scaled_base(runtime_base, ai_risk_review.risk_scale)
                    runtime_config = _replace_base(runtime_config, runtime_base)
                    signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, side)
                    expected_action = _expected_action(strategy)
                    if signal.action != expected_action:
                        index += 1
                        continue

        throttle_key = _journal_throttle_key(strategy, decision, market_decision)
        if (
            journal_throttle_enabled
            and _journal_throttle_strategy_in_scope(strategy, journal_throttle_strategy_scope)
            and _journal_throttle_blocks(
                equity_base.equity_usdc,
                day,
                throttle_key,
                throttle_state,
                journal_throttle_max_losses,
                journal_throttle_loss_pct,
            )
        ):
            if journal_throttle_risk_scale <= 0:
                index += 1
                continue
            runtime_base = _scaled_base(runtime_base, journal_throttle_risk_scale)
            runtime_config = _replace_base(runtime_config, runtime_base)
            signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, side)
            expected_action = _expected_action(strategy)
            if signal.action != expected_action:
                index += 1
                continue
            throttle_key = _journal_throttle_key(strategy, decision, market_decision)

        if strategy in {"orb_short", "vwap_short"}:
            trade_config = _strategy_trade_config(
                runtime_config,
                strategy,
                decision,
                short_max_holding_bars,
                vwap_max_holding_bars,
                regime_exit_profile_enabled,
                defensive_exit_weights,
                defensive_breakeven_after_tp,
                defensive_breakeven_lock_r,
                defensive_max_holding_bars,
                defensive_exit_scope,
            )
            trade, next_index = simulate_orb_short(candles, index + 1, signal, trade_config)
        else:
            trade_config = _strategy_trade_config(
                runtime_config,
                strategy,
                decision,
                short_max_holding_bars,
                vwap_max_holding_bars,
                regime_exit_profile_enabled,
                defensive_exit_weights,
                defensive_breakeven_after_tp,
                defensive_breakeven_lock_r,
                defensive_max_holding_bars,
                defensive_exit_scope,
            )
            trade, next_index = _simulate_breakout(candles, index + 1, signal, _to_breakout_proxy(trade_config))
        if trade is None:
            index += max(next_index - index, 1)
            continue

        trades.append(trade)
        if decision is not None:
            rows.append(
                _journal_row(
                    base.symbol,
                    strategy,
                    candles[index],
                    signal,
                    trade,
                    decision,
                    market_decision,
                    nim_review,
                    ai_risk_review,
                    allocator_state,
                    allocator_profile,
                    allocator_scale,
                    runtime_base.risk_per_trade_pct,
                    runtime_base.max_position_margin_pct,
                )
            )
        exit_day = _day_key(trade.exit_time_ms)
        daily[exit_day] = daily.get(exit_day, 0.0) + trade.pnl_usdc
        if journal_throttle_enabled and _journal_throttle_strategy_in_scope(strategy, journal_throttle_strategy_scope):
            _journal_throttle_update(exit_day, throttle_key, throttle_state, trade.pnl_usdc)
        equity += trade.pnl_usdc
        peak_equity = max(peak_equity, equity)
        max_drawdown = min(max_drawdown, equity - peak_equity)
        max_drawdown_pct = min(max_drawdown_pct, _drawdown_pct(equity, peak_equity))
        cooldown = max(cooldown, runtime_base.cooldown_bars)
        if trade.pnl_usdc < 0:
            consecutive_losses += 1
            if (
                base.max_consecutive_losses_before_cooldown > 0
                and consecutive_losses >= base.max_consecutive_losses_before_cooldown
            ):
                cooldown = max(cooldown, base.consecutive_loss_cooldown_bars)
                consecutive_losses = 0
        else:
            consecutive_losses = 0
        index = max(next_index, index + 1)

    summary = _summary(base, trades, max_drawdown, max_drawdown_pct, daily)
    return summary, rows


def _rolling_loss_guard(
    base: StrategyConfig,
    day: str,
    daily: dict[str, float],
    lookback_days: int,
    pause_pct: float,
) -> bool:
    if lookback_days <= 0 or pause_pct <= 0:
        return False
    current = date.fromisoformat(day)
    rolling_pnl = 0.0
    for offset in range(1, lookback_days + 1):
        rolling_pnl += daily.get((current - timedelta(days=offset)).isoformat(), 0.0)
    return rolling_pnl <= -(base.equity_usdc * pause_pct / 100)


def _journal_throttle_key(strategy: str, regime_decision, market_decision) -> tuple[str, ...]:
    return (
        strategy,
        getattr(regime_decision, "regime", "unknown"),
        getattr(regime_decision, "risk_mode", "unknown"),
        getattr(market_decision, "playbook", "unknown"),
        getattr(market_decision, "risk_mode", "unknown"),
    )


def _journal_throttle_strategy_in_scope(strategy: str, scope: str) -> bool:
    if scope == "short":
        return strategy in {"orb_short", "vwap_short"}
    if scope == "long":
        return strategy not in {"orb_short", "vwap_short"}
    return True


def _journal_throttle_blocks(
    equity_base: float,
    day: str,
    key: tuple[str, ...],
    state: dict[tuple[str, tuple[str, ...]], dict[str, float | int]],
    max_losses: int,
    loss_pct: float,
) -> bool:
    record = state.get((day, key))
    if record is None:
        return False
    if max_losses > 0 and int(record.get("losses", 0)) >= max_losses:
        return True
    if loss_pct > 0 and float(record.get("pnl", 0.0)) <= -(equity_base * loss_pct / 100):
        return True
    return False


def _journal_throttle_update(
    day: str,
    key: tuple[str, ...],
    state: dict[tuple[str, tuple[str, ...]], dict[str, float | int]],
    pnl_usdc: float,
) -> None:
    record = state.setdefault((day, key), {"pnl": 0.0, "losses": 0})
    record["pnl"] = float(record.get("pnl", 0.0)) + pnl_usdc
    if pnl_usdc < 0:
        record["losses"] = int(record.get("losses", 0)) + 1


def _local_nim_policy_review(policy: str, strategy: str, signal: SignalPlan, market_decision) -> LocalNimReview | None:
    if policy == "all":
        return None
    if market_decision is None:
        return None
    features = market_decision.features
    signal_playbook = _strategy_playbook(strategy)

    if features.volume_ratio < 0.35:
        return LocalNimReview("no_trade", "off", 0.84, ("local_volume_too_thin",))
    if features.volume_ratio < 0.55:
        return LocalNimReview("no_trade", "small", 0.78, ("local_thin_volume_small_size",))
    if signal.score < 65:
        return LocalNimReview("no_trade", "off", 0.82, ("local_score_too_low",))

    aligned = market_decision.playbook == signal_playbook
    strong_score = signal.score >= 96
    healthy_volume = features.volume_ratio >= 0.85
    very_strong_volume = features.volume_ratio >= 1.5
    if aligned and strong_score and healthy_volume:
        risk_mode = "aggressive" if very_strong_volume and signal.score >= 98 else "normal"
        return LocalNimReview(signal_playbook, risk_mode, 0.84, ("local_clear_accept_aligned",))

    if strategy == "orb_long" and market_decision.trend == "up" and signal.score >= 94 and healthy_volume:
        if market_decision.playbook == "no_trade" and 2.0 <= features.volume_ratio < 3.2:
            return None
        risk_mode = "aggressive" if signal.score >= 98 and features.volume_ratio >= 3.2 else "normal"
        return LocalNimReview("long_pullback", risk_mode, 0.80, ("local_clear_accept_uptrend",))
    if strategy == "orb_short" and market_decision.volatility == "high" and signal.score >= 94:
        return None

    if market_decision.playbook == "no_trade" and signal.score >= 84:
        return None
    if market_decision.risk_mode == "off" and signal.score < 84:
        return LocalNimReview("no_trade", "small", 0.70, ("local_soft_reject_off_regime",))

    return None


def _strategy_playbook(strategy: str) -> str:
    if strategy in {"vwap_long", "vwap_short"}:
        return "vwap_reversion"
    if strategy == "orb_short":
        return "short_breakdown"
    return "long_breakout"


def _expected_action(strategy: str) -> str:
    return "PLAN_SHORT" if strategy in {"orb_short", "vwap_short"} else "PLAN_LONG"


def _nim_review_rejected_by_market_state(strategy: str, regime_decision, market_decision, review) -> bool:
    if strategy != "orb_long":
        return False
    if market_decision.playbook == "vwap_reversion":
        return True
    regime_features = regime_decision.features
    if regime_decision.regime == "chop":
        return True
    if (
        market_decision.playbook == "long_pullback"
        and market_decision.n_pattern == "none"
        and market_decision.breakout_quality == "weak"
        and market_decision.pullback_quality == "healthy"
        and regime_features.close_position_lookback >= 0.70
    ):
        return True
    if (
        market_decision.playbook == "long_pullback"
        and market_decision.n_pattern == "none"
        and market_decision.breakout_quality == "weak"
        and market_decision.pullback_quality == "healthy"
        and review.playbook == "long_pullback"
        and review.risk_mode == "small"
        and review.confidence <= 0.55
        and market_decision.features.volume_ratio < 1.10
    ):
        return True
    if (
        regime_decision.regime == "trend_up"
        and regime_decision.risk_mode == "normal"
        and market_decision.playbook == "no_trade"
        and market_decision.risk_mode == "off"
        and review.playbook == "long_pullback"
        and review.risk_mode == "normal"
    ):
        return True
    if (
        regime_decision.regime == "high_volatility"
        and market_decision.playbook == "no_trade"
        and market_decision.risk_mode == "off"
        and review.playbook == "long_pullback"
        and review.risk_mode == "normal"
    ):
        return True
    if (
        market_decision.playbook == "no_trade"
        and market_decision.risk_mode == "off"
        and market_decision.n_pattern == "none"
        and market_decision.pullback_quality == "none"
        and review.playbook == "long_pullback"
        and review.risk_mode == "aggressive"
    ):
        return True
    if review.playbook != "long_pullback" or review.risk_mode not in {"normal", "aggressive"}:
        return False
    late_trend_up_pullback = (
        regime_decision.regime == "trend_up"
        and regime_decision.risk_mode in {"normal", "aggressive"}
        and market_decision.n_pattern == "none"
        and regime_features.close_position_lookback >= 0.68
    )
    if not late_trend_up_pullback:
        return False
    if market_decision.playbook == "long_pullback":
        return market_decision.breakout_quality == "weak" and market_decision.pullback_quality == "healthy"
    if market_decision.playbook != "no_trade" or market_decision.risk_mode != "off":
        return False
    return (
        (market_decision.breakout_quality == "weak" and market_decision.pullback_quality == "deep")
        or (market_decision.breakout_quality == "strong" and market_decision.pullback_quality == "deep")
    )


def _nim_scaled_base(
    base: StrategyConfig,
    review,
    *,
    hard_block_enabled: bool = True,
) -> StrategyConfig | None:
    if (
        hard_block_enabled
        and review.playbook == "no_trade"
        and review.risk_mode == "off"
        and review.confidence >= 0.82
    ):
        return None
    if review.playbook == "no_trade":
        if review.risk_mode == "off" and not hard_block_enabled:
            scale = 0.25
        else:
            scale = 0.25 if review.risk_mode in {"small", "off"} else 0.35
    elif review.playbook == "long_breakout" and review.risk_mode == "small":
        scale = 0.12
    elif review.risk_mode == "aggressive":
        scale = 1.20
    elif review.risk_mode == "normal":
        scale = 1.0
    elif review.risk_mode == "small":
        scale = 0.65
    elif review.risk_mode == "off":
        scale = 0.45
    else:
        scale = 0.75
    if abs(scale - 1.0) < 1e-9:
        return base
    from dataclasses import replace

    return replace(
        base,
        risk_per_trade_pct=base.risk_per_trade_pct * scale,
        max_position_margin_pct=base.max_position_margin_pct * scale,
        accelerator_risk_per_trade_pct=base.accelerator_risk_per_trade_pct * scale,
        accelerator_margin_pct=base.accelerator_margin_pct * scale,
        accelerator_enabled=base.accelerator_enabled and scale >= 0.5,
    )


def _nim_cache_key(symbol: str, candle: Candle, strategy: str, signal: SignalPlan) -> str:
    return f"v2:{symbol}:{candle.open_time_ms}:{strategy}:{signal.score}:{round(signal.price, 2)}"


def _ai_risk_cache_key(
    symbol: str,
    candle: Candle,
    strategy: str,
    signal: SignalPlan,
    allocator_state: str,
    allocator_profile: str,
) -> str:
    return f"risk:v2:{symbol}:{candle.open_time_ms}:{strategy}:{signal.score}:{allocator_state}:{allocator_profile}:{round(signal.price, 2)}"


def _nim_candidate_payload(strategy: str, signal: SignalPlan) -> dict:
    return {
        "strategy": strategy,
        "action": signal.action,
        "score": signal.score,
        "confidence": signal.confidence,
        "price": round(signal.price, 4),
        "planned_margin_usdc": round(signal.planned_margin_usdc, 4),
        "planned_notional_usdc": round(signal.planned_notional_usdc, 4),
        "leverage_cap": round(signal.leverage_cap, 4),
        "reasons": list(signal.reasons),
        "risk_notes": list(signal.risk_notes),
    }


def _ai_risk_candidate_payload(
    strategy: str,
    signal: SignalPlan,
    allocator_state: str,
    allocator_profile: str,
    allocator_scale: float,
    runtime_base: StrategyConfig,
    day_pnl: float,
) -> dict:
    return {
        **_nim_candidate_payload(strategy, signal),
        "allocator_state": allocator_state,
        "allocator_profile": allocator_profile,
        "allocator_scale": round(allocator_scale, 4),
        "allocated_risk_pct": round(runtime_base.risk_per_trade_pct, 4),
        "allocated_margin_pct": round(runtime_base.max_position_margin_pct, 4),
        "day_pnl_usdc": round(day_pnl, 4),
        "equity_usdc": round(runtime_base.equity_usdc, 4),
    }


def _ai_risk_judge_targets(allocator_state: str, allocator_profile: str) -> bool:
    if allocator_profile == "short_breakdown":
        return True
    if allocator_profile == "trend_up_normal":
        return True
    return allocator_state == "protect" and allocator_profile == "trend_up_aggressive"


def _ai_risk_judge_ai_query_needed(
    strategy: str,
    market_decision,
    allocator_state: str,
    allocator_profile: str,
    runtime_base: StrategyConfig,
) -> bool:
    if not _ai_risk_judge_targets(allocator_state, allocator_profile):
        return False
    allocated_risk_pct = runtime_base.risk_per_trade_pct
    market_features = getattr(market_decision, "features", None)
    volume_ratio = getattr(market_features, "volume_ratio", 1.0)
    market_close_position = getattr(market_features, "close_position_20", 0.5)
    if strategy == "orb_short" and allocator_profile == "short_breakdown":
        return allocated_risk_pct >= 40
    if strategy == "orb_long" and allocator_state == "protect" and allocator_profile == "trend_up_aggressive":
        return allocated_risk_pct >= 45
    if strategy == "orb_long" and allocator_profile == "trend_up_normal":
        if allocated_risk_pct >= 50:
            return True
        return (
            allocated_risk_pct >= 25
            and getattr(market_decision, "playbook", "") == "long_pullback"
            and getattr(market_decision, "breakout_quality", "") == "weak"
            and volume_ratio < 1.2
            and market_close_position >= 0.70
        )
    return False


def _local_ai_risk_review(
    strategy: str,
    regime_decision,
    market_decision,
    signal: SignalPlan,
    allocator_state: str,
    allocator_profile: str,
    runtime_base: StrategyConfig,
) -> LocalAiRiskReview | None:
    if not _ai_risk_judge_targets(allocator_state, allocator_profile):
        return None
    market_features = getattr(market_decision, "features", None)
    regime_features = getattr(regime_decision, "features", None)
    if market_features is None or regime_features is None:
        return None
    allocated_risk_pct = runtime_base.risk_per_trade_pct
    close_position = getattr(regime_features, "close_position_lookback", 0.5)
    market_close_position = getattr(market_features, "close_position_20", 0.5)
    atr_percentile = getattr(market_features, "atr_percentile", 0.5)
    volume_ratio = getattr(market_features, "volume_ratio", 1.0)

    if strategy == "orb_short" and allocator_profile == "short_breakdown":
        if atr_percentile <= 0.25 and close_position >= 0.45:
            return LocalAiRiskReview(
                "reject",
                "extreme",
                0.0,
                0.86,
                ("local_short_breakdown_low_atr_mid_structure_reversal_risk",),
            )
        if allocated_risk_pct >= 50 and market_close_position >= 0.10 and atr_percentile <= 0.45 and volume_ratio >= 5.0:
            return LocalAiRiskReview(
                "reduce",
                "high",
                0.35,
                0.74,
                ("local_short_breakdown_volume_spike_tail_risk",),
            )

    if strategy == "orb_long" and allocator_state == "protect" and allocator_profile == "trend_up_aggressive":
        if allocated_risk_pct >= 60 and close_position >= 0.90 and atr_percentile <= 0.55:
            return LocalAiRiskReview(
                "reject",
                "extreme",
                0.0,
                0.82,
                ("local_protect_aggressive_long_overextended_after_loss",),
            )
        if allocator_state == "protect" and allocated_risk_pct > 45:
            return LocalAiRiskReview(
                "reduce",
                "high",
                0.50,
                0.72,
                ("local_protect_position_risk_cap",),
            )

    if strategy == "orb_long" and allocator_profile == "trend_up_normal":
        if (
            allocated_risk_pct >= 50
            and getattr(market_decision, "playbook", "") == "long_pullback"
            and getattr(market_decision, "breakout_quality", "") == "weak"
            and volume_ratio < 1.0
            and close_position >= 0.75
        ):
            return LocalAiRiskReview(
                "reject",
                "extreme",
                0.0,
                0.80,
                ("local_high_risk_long_pullback_weak_volume_overextended",),
            )

    return None


def _bounded_external_ai_risk_review(review) -> LocalAiRiskReview:
    reason_codes = tuple(getattr(review, "reason_codes", ()))
    if any(str(code).startswith("local_") for code in reason_codes):
        return review
    decision = getattr(review, "decision", "accept")
    risk_level = getattr(review, "risk_level", "medium")
    risk_scale = float(getattr(review, "risk_scale", 1.0))
    confidence = float(getattr(review, "confidence", 0.0))
    if decision == "reject":
        return LocalAiRiskReview(
            "reduce",
            "high" if risk_level == "extreme" else risk_level,
            0.50,
            confidence,
            ("external_ai_reject_downgraded_to_reduce",) + reason_codes,
        )
    if decision == "reduce":
        return LocalAiRiskReview(
            "reduce",
            risk_level,
            max(risk_scale, 0.70),
            confidence,
            ("external_ai_reduce_floor_070",) + reason_codes,
        )
    return LocalAiRiskReview(
        "accept",
        risk_level,
        1.0,
        confidence,
        reason_codes,
    )


def _market_state_allows(strategy: str, market_decision) -> bool:
    if market_decision is None:
        return False
    if market_decision.risk_mode == "off" or market_decision.playbook == "no_trade":
        return False
    if strategy == "orb_long":
        return market_decision.playbook in {"long_breakout", "long_pullback"}
    if strategy == "orb_short":
        return market_decision.playbook == "short_breakdown"
    return False


def _market_state_scaled_base(base: StrategyConfig, strategy: str, market_decision) -> StrategyConfig | None:
    if market_decision is None:
        return base
    aligned = _market_state_allows(strategy, market_decision)
    if market_decision.risk_mode == "off" and market_decision.playbook == "no_trade":
        return None
    if market_decision.risk_mode == "aggressive" and aligned:
        scale = 1.25
    elif market_decision.risk_mode == "normal" and aligned:
        scale = 1.0
    elif market_decision.risk_mode == "small" and aligned:
        scale = 0.55
    elif market_decision.playbook == "no_trade":
        scale = 0.35
    else:
        scale = 0.60
    if abs(scale - 1.0) < 1e-9:
        return base
    from dataclasses import replace

    return replace(
        base,
        risk_per_trade_pct=base.risk_per_trade_pct * scale,
        max_position_margin_pct=base.max_position_margin_pct * scale,
        accelerator_risk_per_trade_pct=base.accelerator_risk_per_trade_pct * scale,
        accelerator_margin_pct=base.accelerator_margin_pct * scale,
        accelerator_enabled=base.accelerator_enabled and scale >= 0.5,
    )


def _short_quality_filter_blocks(strategy: str, regime_decision, market_decision) -> bool:
    if strategy != "orb_short" or market_decision is None:
        return False
    if market_decision.breakout_quality == "fake_risk":
        return True
    return False


def _short_exhaustion_confirmed(regime_decision, market_decision) -> bool:
    if regime_decision is None or market_decision is None:
        return False
    if getattr(regime_decision, "regime", "") not in {"trend_down", "high_volatility"}:
        return False
    features = getattr(regime_decision, "features", None)
    market_features = getattr(market_decision, "features", None)
    if features is None or market_features is None:
        return False
    return (
        getattr(features, "close_position_lookback", 1.0) <= 0.30
        and getattr(features, "trend_slope_atr", 0.0) <= -1.25
        and getattr(market_features, "volume_ratio", 0.0) >= 1.10
    )


def _short_weak_low_atr_confirmed(regime_decision, market_decision) -> bool:
    if regime_decision is None or market_decision is None:
        return False
    if getattr(regime_decision, "regime", "") != "trend_down":
        return False
    if getattr(market_decision, "ma20_structure", "") != "below_falling":
        return False
    if getattr(market_decision, "breakout_quality", "") != "weak":
        return False
    features = getattr(regime_decision, "features", None)
    market_features = getattr(market_decision, "features", None)
    if features is None or market_features is None:
        return False
    volume_ratio = getattr(market_features, "volume_ratio", 0.0)
    close_position = getattr(features, "close_position_lookback", 0.0)
    return (
        getattr(market_features, "atr_percentile", 1.0) <= 0.50
        and 0.80 <= volume_ratio < 2.00
        and 0.20 < close_position < 0.80
    )


def _regime_allocator_adjusted_base(
    base: StrategyConfig,
    strategy: str,
    regime_decision,
    market_decision,
    nim_review,
    day_pnl: float,
    protect_loss_pct: float,
    lock_profit_pct: float,
    protect_scale: float,
    lock_scale: float,
    trend_aggressive_scale: float,
    trend_normal_scale: float,
    trend_normal_low_quality_scale: float | None,
    trend_normal_weak_scale: float,
    short_scale: float,
    short_weak_low_atr_scale: float | None,
    short_fake_risk_scale: float | None,
    short_exhaustion_scale: float | None,
    short_exhaustion_strong_scale: float | None,
    short_breakdown_scale: float,
    volatility_short_breakdown_scale: float,
    reversion_scale: float,
    weak_pullback_scale: float,
    weak_pullback_normal_scale: float | None,
    aggressive_no_trade_scale: float,
    max_risk_pct: float,
    max_margin_pct: float,
    signal_score: int = 0,
) -> tuple[StrategyConfig | None, dict]:
    state = _allocator_daily_state(base, day_pnl, protect_loss_pct, lock_profit_pct)
    profile = _allocator_profile(strategy, regime_decision, market_decision, nim_review, signal_score)
    scale = _allocator_profile_scale(
        profile,
        trend_aggressive_scale,
        trend_normal_scale,
        trend_normal_low_quality_scale,
        trend_normal_weak_scale,
        short_scale,
        short_weak_low_atr_scale,
        short_fake_risk_scale,
        short_exhaustion_scale,
        short_exhaustion_strong_scale,
        short_breakdown_scale,
        volatility_short_breakdown_scale,
        reversion_scale,
        weak_pullback_scale,
        aggressive_no_trade_scale,
    )
    if state == "normal" and profile == "weak_pullback_small" and weak_pullback_normal_scale is not None:
        scale = weak_pullback_normal_scale
    if state == "protect":
        scale *= protect_scale
    elif state == "lock_profit":
        scale *= lock_scale
    if scale <= 0.0:
        return None, {"state": state, "profile": profile, "scale": 0.0}
    scale = max(min(scale, 1.5), 0.05)
    allocation = {"state": state, "profile": profile, "scale": round(scale, 4)}
    if abs(scale - 1.0) < 1e-9:
        return _capped_allocator_base(base, max_risk_pct, max_margin_pct), allocation
    return _capped_allocator_base(_scaled_base(base, scale), max_risk_pct, max_margin_pct), allocation


def _allocator_daily_state(
    base: StrategyConfig,
    day_pnl: float,
    protect_loss_pct: float,
    lock_profit_pct: float,
) -> str:
    if base.equity_usdc <= 0:
        return "normal"
    day_pnl_pct = day_pnl / base.equity_usdc * 100
    if protect_loss_pct > 0 and day_pnl_pct <= -protect_loss_pct:
        return "protect"
    if lock_profit_pct > 0 and day_pnl_pct >= lock_profit_pct:
        return "lock_profit"
    return "normal"


def _allocator_profile(strategy: str, regime_decision, market_decision, nim_review=None, signal_score: int = 0) -> str:
    if strategy in {"vwap_long", "vwap_short"}:
        return "reversion"
    if strategy == "orb_short":
        if getattr(market_decision, "breakout_quality", "") == "fake_risk":
            return "short_fake_risk"
        if _short_exhaustion_confirmed(regime_decision, market_decision):
            if getattr(market_decision, "breakout_quality", "") == "strong":
                return "short_exhaustion_strong"
            return "short_exhaustion"
        if _short_weak_low_atr_confirmed(regime_decision, market_decision):
            return "short_weak_low_atr"
        if (
            getattr(market_decision, "playbook", "") == "short_breakdown"
            or getattr(nim_review, "playbook", "") == "short_breakdown"
        ):
            if getattr(regime_decision, "regime", "") == "trend_down":
                return "short_breakdown"
            if getattr(regime_decision, "regime", "") == "high_volatility":
                return "volatility_short_breakdown"
        return "short"
    if regime_decision is None:
        return "unknown"
    if (
        strategy == "orb_long"
        and market_decision is not None
        and nim_review is not None
        and regime_decision.regime == "trend_up"
        and regime_decision.risk_mode == "aggressive"
        and market_decision.playbook == "no_trade"
        and market_decision.risk_mode == "off"
        and nim_review.playbook == "long_pullback"
    ):
        if _aggressive_no_trade_pullback_confirmed(regime_decision, market_decision, nim_review):
            return "trend_up_aggressive"
        return "aggressive_no_trade_pullback"
    if (
        strategy == "orb_long"
        and market_decision is not None
        and nim_review is not None
        and market_decision.playbook == "long_pullback"
        and market_decision.n_pattern == "none"
        and market_decision.breakout_quality == "weak"
        and market_decision.pullback_quality == "healthy"
        and nim_review.playbook == "long_pullback"
        and nim_review.risk_mode == "small"
    ):
        return "weak_pullback_small"
    if (
        strategy == "orb_long"
        and regime_decision.regime == "trend_up"
        and regime_decision.risk_mode == "normal"
        and market_decision is not None
        and market_decision.playbook == "no_trade"
    ):
        if _trend_up_normal_low_quality_confirmed(regime_decision, market_decision):
            return "trend_up_normal_low_quality"
        if _trend_up_normal_no_trade_confirmed(regime_decision, market_decision, signal_score):
            return "trend_up_normal"
        return "trend_up_normal_weak"
    if strategy == "orb_long" and regime_decision.regime == "trend_up":
        return "trend_up_aggressive" if regime_decision.risk_mode == "aggressive" else "trend_up_normal"
    if strategy == "orb_long" and regime_decision.regime == "high_volatility":
        return "volatility_long"
    if strategy == "orb_long" and regime_decision.regime in {"range", "chop", "low_liquidity"}:
        return "exploratory_long"
    if market_decision is not None and market_decision.playbook == "no_trade":
        return "no_trade"
    return f"{strategy}_{regime_decision.regime}"


def _aggressive_no_trade_pullback_confirmed(regime_decision, market_decision, nim_review) -> bool:
    if getattr(market_decision, "n_pattern", "none") == "bullish":
        return True
    if getattr(nim_review, "risk_mode", "small") == "aggressive":
        return True
    if getattr(market_decision, "breakout_quality", "weak") == "strong":
        return True
    features = getattr(regime_decision, "features", None)
    if features is None:
        return False
    return (
        getattr(features, "close_position_lookback", 0.0) >= 0.88
        and getattr(features, "trend_slope_atr", 0.0) >= 1.50
    )


def _trend_up_normal_no_trade_confirmed(regime_decision, market_decision, signal_score: int) -> bool:
    market_features = getattr(market_decision, "features", None)
    regime_features = getattr(regime_decision, "features", None)
    volume_ratio = getattr(market_features, "volume_ratio", 0.0)
    close_position = getattr(regime_features, "close_position_lookback", 0.0)
    trend_slope = getattr(regime_features, "trend_slope_atr", 0.0)
    return signal_score >= 90 and volume_ratio >= 1.05 and close_position >= 0.45 and trend_slope >= 0.15


def _trend_up_normal_low_quality_confirmed(regime_decision, market_decision) -> bool:
    market_features = getattr(market_decision, "features", None)
    regime_features = getattr(regime_decision, "features", None)
    if market_features is None or regime_features is None:
        return False
    close_position = getattr(regime_features, "close_position_lookback", 0.0)
    return (
        getattr(market_decision, "breakout_quality", "") == "weak"
        and 0.25 <= getattr(market_features, "atr_percentile", 0.0) <= 0.50
        and 0.55 < close_position < 0.85
    )


def _allocator_profile_scale(
    profile: str,
    trend_aggressive_scale: float,
    trend_normal_scale: float,
    trend_normal_low_quality_scale: float | None,
    trend_normal_weak_scale: float,
    short_scale: float,
    short_weak_low_atr_scale: float | None,
    short_fake_risk_scale: float | None,
    short_exhaustion_scale: float | None,
    short_exhaustion_strong_scale: float | None,
    short_breakdown_scale: float,
    volatility_short_breakdown_scale: float,
    reversion_scale: float,
    weak_pullback_scale: float,
    aggressive_no_trade_scale: float,
) -> float:
    if profile == "aggressive_no_trade_pullback":
        return aggressive_no_trade_scale
    if profile == "weak_pullback_small":
        return weak_pullback_scale
    if profile == "trend_up_aggressive":
        return trend_aggressive_scale
    if profile == "trend_up_normal":
        return trend_normal_scale
    if profile == "trend_up_normal_low_quality":
        return trend_normal_scale if trend_normal_low_quality_scale is None else trend_normal_low_quality_scale
    if profile == "trend_up_normal_weak":
        return trend_normal_weak_scale
    if profile == "short_breakdown":
        return short_breakdown_scale
    if profile == "volatility_short_breakdown":
        return volatility_short_breakdown_scale
    if profile == "short":
        return short_scale
    if profile == "short_weak_low_atr":
        return short_scale if short_weak_low_atr_scale is None else short_weak_low_atr_scale
    if profile == "short_fake_risk":
        return short_scale if short_fake_risk_scale is None else short_fake_risk_scale
    if profile == "short_exhaustion":
        return short_scale if short_exhaustion_scale is None else short_exhaustion_scale
    if profile == "short_exhaustion_strong":
        if short_exhaustion_strong_scale is not None:
            return short_exhaustion_strong_scale
        return short_scale if short_exhaustion_scale is None else short_exhaustion_scale
    if profile == "reversion":
        return reversion_scale
    if profile in {"volatility_long", "exploratory_long"}:
        return reversion_scale
    if profile == "no_trade":
        return min(trend_normal_scale, short_scale, reversion_scale)
    return 1.0


def _capped_allocator_base(base: StrategyConfig, max_risk_pct: float, max_margin_pct: float) -> StrategyConfig:
    if (
        (max_risk_pct <= 0 or base.risk_per_trade_pct <= max_risk_pct)
        and (max_margin_pct <= 0 or base.max_position_margin_pct <= max_margin_pct)
    ):
        return base
    from dataclasses import replace

    risk_scale = max_risk_pct / base.risk_per_trade_pct if max_risk_pct > 0 and base.risk_per_trade_pct > max_risk_pct else 1.0
    margin_scale = (
        max_margin_pct / base.max_position_margin_pct
        if max_margin_pct > 0 and base.max_position_margin_pct > max_margin_pct
        else 1.0
    )
    accelerator_scale = min(risk_scale, margin_scale)
    return replace(
        base,
        risk_per_trade_pct=min(base.risk_per_trade_pct, max_risk_pct) if max_risk_pct > 0 else base.risk_per_trade_pct,
        max_position_margin_pct=min(base.max_position_margin_pct, max_margin_pct) if max_margin_pct > 0 else base.max_position_margin_pct,
        accelerator_risk_per_trade_pct=base.accelerator_risk_per_trade_pct * accelerator_scale,
        accelerator_margin_pct=base.accelerator_margin_pct * accelerator_scale,
        accelerator_enabled=base.accelerator_enabled and accelerator_scale >= 0.5,
    )


def _regime_router_adjusted_base(
    base: StrategyConfig,
    strategy: str,
    regime_decision,
    market_decision,
    defensive_scale: float,
    exploratory_scale: float,
) -> StrategyConfig | None:
    if regime_decision is None or market_decision is None:
        return base
    if strategy in {"vwap_long", "vwap_short"}:
        if regime_decision.regime not in {"range", "chop"}:
            return None
        return _scaled_base(base, exploratory_scale)
    if strategy == "orb_short":
        if regime_decision.regime not in {"trend_down", "high_volatility"}:
            return None
        if market_decision.playbook == "vwap_reversion":
            return None
        scale = 1.0
        if regime_decision.risk_mode == "off":
            scale = defensive_scale
        if market_decision.playbook == "no_trade" and market_decision.risk_mode == "off":
            scale = min(scale, defensive_scale)
        if regime_decision.regime == "high_volatility":
            scale = min(scale, exploratory_scale)
        if scale <= 0:
            return None
        if abs(scale - 1.0) < 1e-9:
            return base
        return _scaled_base(base, scale)
    if strategy != "orb_long":
        return base
    if regime_decision.regime == "chop" or market_decision.playbook == "vwap_reversion":
        return None
    if (
        market_decision.playbook == "no_trade"
        and market_decision.risk_mode == "off"
        and market_decision.n_pattern == "none"
        and market_decision.pullback_quality == "none"
        and market_decision.features.volume_ratio >= 3.0
    ):
        return None
    scale = 1.0
    if regime_decision.regime in {"range", "low_liquidity"}:
        scale = exploratory_scale
    elif regime_decision.regime == "high_volatility" and market_decision.playbook == "no_trade":
        scale = defensive_scale
    elif regime_decision.regime == "trend_up" and regime_decision.risk_mode == "normal":
        if market_decision.playbook == "no_trade" and market_decision.risk_mode == "off":
            scale = exploratory_scale
        elif market_decision.n_pattern == "none" and market_decision.breakout_quality == "weak":
            scale = defensive_scale
    elif regime_decision.regime == "trend_up" and regime_decision.risk_mode == "aggressive":
        if market_decision.n_pattern == "bullish":
            scale = 1.0
        elif market_decision.playbook == "no_trade" and market_decision.risk_mode == "off":
            scale = defensive_scale
    if scale <= 0:
        return None
    if abs(scale - 1.0) < 1e-9:
        return base
    return _scaled_base(base, scale)


def _scaled_base(base: StrategyConfig, scale: float) -> StrategyConfig:
    from dataclasses import replace

    return replace(
        base,
        risk_per_trade_pct=base.risk_per_trade_pct * scale,
        max_position_margin_pct=base.max_position_margin_pct * scale,
        accelerator_risk_per_trade_pct=base.accelerator_risk_per_trade_pct * scale,
        accelerator_margin_pct=base.accelerator_margin_pct * scale,
        accelerator_enabled=base.accelerator_enabled and scale >= 0.5,
    )


def _select_journal_signal(
    candles: list[Candle],
    index: int,
    config: OrbConfig,
    context,
    decision,
    side: Literal["long", "short", "both", "router"],
) -> tuple[SignalPlan, str]:
    if side == "short":
        return generate_orb_short_signal_at(candles, index, config, context), "orb_short"
    if side == "long":
        return generate_orb_signal_at(candles, index, config, context), "orb_long"
    if side == "router":
        return _select_router_signal(candles, index, config, context, decision)

    long_signal = generate_orb_signal_at(candles, index, config, context)
    short_signal = generate_orb_short_signal_at(candles, index, config, context)
    long_ok = long_signal.action == "PLAN_LONG" and _both_side_allows("orb_long", decision, long_signal)
    short_ok = short_signal.action == "PLAN_SHORT" and _both_side_allows("orb_short", decision, short_signal)
    if not long_ok and not short_ok:
        return long_signal, "orb_short"
    if long_ok and not short_ok:
        return long_signal, "orb_long"
    if short_ok and not long_ok:
        return short_signal, "orb_short"

    if decision is not None:
        if decision.regime == "trend_up":
            return long_signal, "orb_long"
        if decision.regime in {"chop", "high_volatility"}:
            return short_signal, "orb_short"
        if decision.regime == "trend_down" and short_signal.score >= long_signal.score + 12:
            return short_signal, "orb_short"

    return (short_signal, "orb_short") if short_signal.score > long_signal.score else (long_signal, "orb_long")


def _select_router_signal(
    candles: list[Candle],
    index: int,
    config: OrbConfig,
    context,
    decision,
) -> tuple[SignalPlan, str]:
    long_signal = generate_orb_signal_at(candles, index, config, context)
    if decision is None:
        return long_signal, "orb_long"
    if decision.regime == "trend_up":
        return long_signal, "orb_long"
    if decision.regime == "trend_down":
        short_signal = generate_orb_short_signal_at(candles, index, config, context)
        if short_signal.action == "PLAN_SHORT":
            return short_signal, "orb_short"
        return long_signal, "orb_long"
    if decision.regime == "high_volatility":
        short_signal = generate_orb_short_signal_at(candles, index, config, context)
        if (
            short_signal.action == "PLAN_SHORT"
            and decision.features.trend_slope_atr < -0.2
            and decision.features.close_position_lookback <= 0.45
        ):
            return short_signal, "orb_short"
        return long_signal, "orb_long"
    if decision.regime in {"range", "chop"}:
        vwap_long = generate_vwap_reversion_long_signal_at(candles, index, config, context)
        vwap_short = generate_vwap_reversion_short_signal_at(candles, index, config, context)
        candidates = [
            (vwap_long, "vwap_long") if vwap_long.action == "PLAN_LONG" else None,
            (vwap_short, "vwap_short") if vwap_short.action == "PLAN_SHORT" else None,
        ]
        valid = [candidate for candidate in candidates if candidate is not None]
        if valid:
            return max(valid, key=lambda item: item[0].score)
    return long_signal, "orb_long"


def _both_side_allows(strategy: str, decision, signal: SignalPlan) -> bool:
    if decision is None:
        return False
    if strategy == "orb_long":
        return (
            (decision.regime == "trend_up" and decision.risk_mode == "aggressive")
            or (decision.regime == "low_liquidity" and decision.risk_mode == "off")
            or (decision.regime == "high_volatility" and decision.risk_mode == "normal")
        )
    return decision.regime == "high_volatility"


def summarize_signal_journal(rows: list[SignalJournalRow]) -> list[dict]:
    buckets: dict[tuple[str, str, str, str, str, str], list[SignalJournalRow]] = {}
    for row in rows:
        buckets.setdefault(
            (row.strategy, row.regime, row.risk_mode, row.market_playbook, row.nim_playbook, row.nim_risk_mode),
            [],
        ).append(row)

    result: list[dict] = []
    for (strategy, regime, risk_mode, market_playbook, nim_playbook, nim_risk_mode), bucket in buckets.items():
        wins = [row.pnl_usdc for row in bucket if row.pnl_usdc > 0]
        losses = [row.pnl_usdc for row in bucket if row.pnl_usdc < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        result.append(
            {
                "strategy": strategy,
                "regime": regime,
                "risk_mode": risk_mode,
                "market_playbook": market_playbook,
                "nim_playbook": nim_playbook,
                "nim_risk_mode": nim_risk_mode,
                "trades": len(bucket),
                "net_pnl_usdc": round(sum(row.pnl_usdc for row in bucket), 4),
                "win_rate_pct": round(len(wins) / len(bucket) * 100, 2) if bucket else 0.0,
                "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else "inf",
                "avg_score": round(sum(row.score for row in bucket) / len(bucket), 2),
                "avg_r_multiple": round(sum(row.r_multiple for row in bucket) / len(bucket), 4),
                "avg_regime_confidence": round(sum(row.regime_confidence for row in bucket) / len(bucket), 4),
                "avg_volume_ratio": round(sum(row.volume_ratio for row in bucket) / len(bucket), 4),
            }
        )
    return sorted(result, key=lambda row: row["net_pnl_usdc"], reverse=True)


def summarize_allocator_journal(rows: list[SignalJournalRow]) -> list[dict]:
    buckets: dict[tuple[str, str, str, str], list[SignalJournalRow]] = {}
    for row in rows:
        buckets.setdefault((row.allocator_state, row.allocator_profile, row.strategy, row.regime), []).append(row)

    result: list[dict] = []
    for (state, profile, strategy, regime), bucket in buckets.items():
        wins = [row.pnl_usdc for row in bucket if row.pnl_usdc > 0]
        losses = [row.pnl_usdc for row in bucket if row.pnl_usdc < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        result.append(
            {
                "allocator_state": state,
                "allocator_profile": profile,
                "strategy": strategy,
                "regime": regime,
                "trades": len(bucket),
                "net_pnl_usdc": round(sum(row.pnl_usdc for row in bucket), 4),
                "planned_margin_usdc": round(sum(row.planned_margin_usdc for row in bucket), 4),
                "planned_notional_usdc": round(sum(row.planned_notional_usdc for row in bucket), 4),
                "avg_allocator_scale": round(sum(row.allocator_scale for row in bucket) / len(bucket), 4),
                "avg_allocated_risk_pct": round(sum(row.allocated_risk_pct for row in bucket) / len(bucket), 4),
                "avg_allocated_margin_pct": round(sum(row.allocated_margin_pct for row in bucket) / len(bucket), 4),
                "win_rate_pct": round(len(wins) / len(bucket) * 100, 2) if bucket else 0.0,
                "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else "inf",
            }
        )
    return sorted(result, key=lambda row: row["net_pnl_usdc"], reverse=True)


def _equity_base(base: StrategyConfig, equity: float) -> StrategyConfig:
    from dataclasses import replace

    return replace(base, equity_usdc=equity) if base.compounding_enabled else base


def _replace_base(config: OrbConfig, base: StrategyConfig) -> OrbConfig:
    from dataclasses import replace

    return replace(config, base=base)


def _strategy_holding_config(
    config: OrbConfig,
    strategy: str,
    short_max_holding_bars: int,
    vwap_max_holding_bars: int,
) -> OrbConfig:
    override = 0
    if strategy in {"vwap_long", "vwap_short"}:
        override = vwap_max_holding_bars
    elif strategy == "orb_short":
        override = short_max_holding_bars
    if override <= 0 or override == config.base.max_holding_bars:
        return config
    from dataclasses import replace

    return replace(config, base=replace(config.base, max_holding_bars=override))


def _strategy_trade_config(
    config: OrbConfig,
    strategy: str,
    decision,
    short_max_holding_bars: int,
    vwap_max_holding_bars: int,
    regime_exit_profile_enabled: bool,
    defensive_exit_weights: tuple[float, float, float],
    defensive_breakeven_after_tp: int,
    defensive_breakeven_lock_r: float,
    defensive_max_holding_bars: int,
    defensive_exit_scope: Literal["non_trend", "short_reversion"] = "non_trend",
) -> OrbConfig:
    config = _strategy_holding_config(config, strategy, short_max_holding_bars, vwap_max_holding_bars)
    if not regime_exit_profile_enabled or not _uses_defensive_exit_profile(strategy, decision, defensive_exit_scope):
        return config

    max_holding_bars = config.base.max_holding_bars
    if defensive_max_holding_bars > 0:
        max_holding_bars = min(max_holding_bars, defensive_max_holding_bars)

    from dataclasses import replace

    return replace(
        config,
        base=replace(
            config.base,
            exit_weights=defensive_exit_weights,
            breakeven_after_tp=defensive_breakeven_after_tp,
            breakeven_lock_r=defensive_breakeven_lock_r,
            max_holding_bars=max_holding_bars,
        ),
    )


def _uses_defensive_exit_profile(
    strategy: str,
    decision,
    scope: Literal["non_trend", "short_reversion"] = "non_trend",
) -> bool:
    if strategy in {"orb_short", "vwap_long", "vwap_short"}:
        return True
    if scope == "short_reversion":
        return False
    if strategy != "orb_long" or decision is None:
        return True
    return not (decision.regime == "trend_up" and decision.risk_mode == "aggressive")


def _reviewer_adjusted_base(
    base: StrategyConfig,
    risk_mode: str,
    small_risk_scale: float,
    aggressive_risk_scale: float,
) -> StrategyConfig:
    scale = 1.0
    if risk_mode == "small":
        scale = max(small_risk_scale, 0.05)
    elif risk_mode == "aggressive":
        scale = min(aggressive_risk_scale, 2.0)
    if abs(scale - 1.0) < 1e-9:
        return base
    from dataclasses import replace

    return replace(
        base,
        risk_per_trade_pct=base.risk_per_trade_pct * scale,
        accelerator_risk_per_trade_pct=base.accelerator_risk_per_trade_pct * scale,
    )


def _journal_row(
    symbol: str,
    strategy: str,
    signal_candle: Candle,
    signal: SignalPlan,
    trade: TradeResult,
    decision,
    market_decision=None,
    nim_review=None,
    ai_risk_review=None,
    allocator_state: str = "not_used",
    allocator_profile: str = "base",
    allocator_scale: float = 1.0,
    allocated_risk_pct: float = 0.0,
    allocated_margin_pct: float = 0.0,
) -> SignalJournalRow:
    features = decision.features
    market_features = market_decision.features if market_decision is not None else None
    return SignalJournalRow(
        symbol=symbol,
        strategy=strategy,
        signal_time_ms=signal_candle.open_time_ms,
        signal_time_iso=datetime.fromtimestamp(signal_candle.open_time_ms / 1000, tz=timezone.utc).isoformat(),
        entry_time_ms=trade.entry_time_ms,
        score=signal.score,
        confidence=signal.confidence,
        planned_margin_usdc=round(signal.planned_margin_usdc, 4),
        planned_notional_usdc=round(signal.planned_notional_usdc, 4),
        leverage_cap=round(signal.leverage_cap, 4),
        allocator_state=allocator_state,
        allocator_profile=allocator_profile,
        allocator_scale=round(allocator_scale, 4),
        allocated_risk_pct=round(allocated_risk_pct, 4),
        allocated_margin_pct=round(allocated_margin_pct, 4),
        regime=decision.regime,
        risk_mode=decision.risk_mode,
        regime_confidence=round(decision.confidence, 4),
        market_playbook=market_decision.playbook if market_decision is not None else "not_used",
        market_risk_mode=market_decision.risk_mode if market_decision is not None else "not_used",
        market_confidence=round(market_decision.confidence, 4) if market_decision is not None else 0.0,
        market_trend=market_decision.trend if market_decision is not None else "not_used",
        market_ma20_structure=market_decision.ma20_structure if market_decision is not None else "not_used",
        market_n_pattern=market_decision.n_pattern if market_decision is not None else "not_used",
        market_breakout_quality=market_decision.breakout_quality if market_decision is not None else "not_used",
        market_pullback_quality=market_decision.pullback_quality if market_decision is not None else "not_used",
        nim_playbook=nim_review.playbook if nim_review is not None else "not_used",
        nim_risk_mode=nim_review.risk_mode if nim_review is not None else "not_used",
        nim_confidence=round(nim_review.confidence, 4) if nim_review is not None else 0.0,
        ai_risk_decision=ai_risk_review.decision if ai_risk_review is not None else "not_used",
        ai_risk_level=ai_risk_review.risk_level if ai_risk_review is not None else "not_used",
        ai_risk_scale=round(ai_risk_review.risk_scale, 4) if ai_risk_review is not None else 1.0,
        ai_risk_confidence=round(ai_risk_review.confidence, 4) if ai_risk_review is not None else 0.0,
        ai_risk_reason_codes=",".join(ai_risk_review.reason_codes) if ai_risk_review is not None else "",
        atr_percentile=round(market_features.atr_percentile, 4) if market_features is not None else round(features.atr_percentile, 4),
        volume_ratio=round(market_features.volume_ratio, 4) if market_features is not None else round(features.volume_ratio, 4),
        trend_slope_atr=round(features.trend_slope_atr, 4),
        close_position_lookback=round(features.close_position_lookback, 4),
        pnl_usdc=round(trade.pnl_usdc, 4),
        r_multiple=round(trade.r_multiple, 4),
        exit_reason=trade.reason,
        hold_bars=trade.hold_bars,
    )
