"""Hybrid long engine: breakout entries gated by N-trend / MA20 structure."""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product

from src.gridbot.strategy.long_breakout import (
    BreakoutConfig,
    BreakoutContext,
    build_breakout_context,
    generate_breakout_signal_at,
    _simulate_breakout,
)
from src.gridbot.strategy.long_ntrend import (
    NTrendConfig,
    NTrendContext,
    build_ntrend_context,
    generate_ntrend_signal_at,
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
    _rank_score,
    _risk_adjusted_config,
    _summary,
)


@dataclass(frozen=True)
class HybridConfig:
    base: StrategyConfig = StrategyConfig()
    breakout: BreakoutConfig = BreakoutConfig()
    ntrend: NTrendConfig = NTrendConfig()
    score_bonus: int = 8


def generate_hybrid_signal(candles: list[Candle], config: HybridConfig | None = None) -> SignalPlan:
    config = config or HybridConfig()
    breakout_context = build_breakout_context(candles, config.breakout)
    ntrend_context = build_ntrend_context(candles, config.ntrend)
    return generate_hybrid_signal_at(candles, len(candles) - 1, config, breakout_context, ntrend_context)


def generate_hybrid_signal_at(
    candles: list[Candle],
    index: int,
    config: HybridConfig,
    breakout_context: BreakoutContext | None = None,
    ntrend_context: NTrendContext | None = None,
) -> SignalPlan:
    breakout_context = breakout_context or build_breakout_context(candles, config.breakout)
    ntrend_context = ntrend_context or build_ntrend_context(candles, config.ntrend)
    breakout_signal = generate_breakout_signal_at(candles, index, config.breakout, breakout_context)
    if breakout_signal.action != "PLAN_LONG":
        return breakout_signal

    ntrend_signal = generate_ntrend_signal_at(candles, index, config.ntrend, ntrend_context)
    if ntrend_signal.action != "PLAN_LONG":
        return SignalPlan(
            action="WAIT",
            confidence=0,
            score=0,
            symbol=breakout_signal.symbol,
            price=breakout_signal.price,
            rsi=breakout_signal.rsi,
            atr=breakout_signal.atr,
            support=breakout_signal.support,
            vwap=breakout_signal.vwap,
            daily_target_usdc=breakout_signal.daily_target_usdc,
            reasons=["breakout rejected by N-trend / MA20 filter"],
            risk_notes=breakout_signal.risk_notes,
        )

    combined_reasons = breakout_signal.reasons + ["N-trend / MA20 filter confirmed"] + ntrend_signal.reasons[:2]
    combined_risk_notes = breakout_signal.risk_notes + ntrend_signal.risk_notes
    return replace(
        breakout_signal,
        score=min(breakout_signal.score + config.score_bonus, 100),
        confidence=min(breakout_signal.confidence + config.score_bonus, 100),
        reasons=combined_reasons,
        risk_notes=combined_risk_notes,
    )


def run_hybrid_backtest(candles: list[Candle], config: HybridConfig | None = None) -> BacktestSummary:
    config = config or HybridConfig()
    base = config.base
    breakout_context = build_breakout_context(candles, config.breakout)
    ntrend_context = build_ntrend_context(candles, config.ntrend)
    warmup = max(
        config.breakout.breakout_lookback,
        config.breakout.volume_lookback,
        config.ntrend.pattern_lookback,
        config.ntrend.ma_period + 3,
        base.ema_slow_period,
    ) + 2
    trades: list[TradeResult] = []
    equity = base.equity_usdc
    peak_equity = equity
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    daily = _empty_daily_pnls(candles)
    consecutive_losses = 0
    cooldown = 0
    index = warmup

    while index < len(candles) - 2:
        if base.max_open_positions < 1:
            break
        if cooldown > 0:
            cooldown -= 1
            index += 1
            continue

        equity_base = replace(base, equity_usdc=equity) if base.compounding_enabled else base
        day = _day_key(candles[index].open_time_ms)
        day_pnl = daily.get(day, 0.0)
        if _daily_guard_reason(equity_base, day_pnl):
            index += 1
            continue

        runtime_base = _risk_adjusted_config(equity_base, day_pnl)
        runtime_breakout = replace(config.breakout, base=runtime_base)
        runtime_ntrend = replace(config.ntrend, base=runtime_base)
        runtime_config = replace(config, base=runtime_base, breakout=runtime_breakout, ntrend=runtime_ntrend)
        signal = generate_hybrid_signal_at(candles, index, runtime_config, breakout_context, ntrend_context)
        if signal.action != "PLAN_LONG":
            index += 1
            continue

        trade, next_index = _simulate_breakout(candles, index + 1, signal, runtime_breakout)
        if trade is None:
            index += max(next_index - index, 1)
            continue

        trades.append(trade)
        exit_day = _day_key(trade.exit_time_ms)
        daily[exit_day] = daily.get(exit_day, 0.0) + trade.pnl_usdc
        equity += trade.pnl_usdc
        peak_equity = max(peak_equity, equity)
        max_drawdown = min(max_drawdown, equity - peak_equity)
        max_drawdown_pct = min(max_drawdown_pct, _drawdown_pct(equity, peak_equity))
        cooldown = max(cooldown, runtime_base.cooldown_bars)
        if trade.pnl_usdc < 0:
            consecutive_losses += 1
            if (
                runtime_base.max_consecutive_losses_before_cooldown > 0
                and consecutive_losses >= runtime_base.max_consecutive_losses_before_cooldown
            ):
                cooldown = max(cooldown, runtime_base.consecutive_loss_cooldown_bars)
                consecutive_losses = 0
        else:
            consecutive_losses = 0
        index = max(next_index, index + 1)

    summary = _summary(base, trades, max_drawdown, max_drawdown_pct, daily)
    return replace(summary, params={
        "strategy": "breakout_ntrend_filter",
        "breakout_lookback": config.breakout.breakout_lookback,
        "ma_period": config.ntrend.ma_period,
        "pattern_lookback": config.ntrend.pattern_lookback,
        "score_bonus": config.score_bonus,
    })


def sweep_hybrid_configs(
    candles: list[Candle],
    base: StrategyConfig | None = None,
    profile: str = "balanced",
) -> list[BacktestSummary]:
    base = base or StrategyConfig()
    if profile == "spec":
        base = replace(
            base,
            daily_soft_loss_pct=4.0,
            daily_max_loss_pct=10.0,
            daily_loss_risk_scale=0.60,
            daily_target_stop_pct=max(base.daily_target_stop_pct, base.daily_target_min_pct),
            max_position_margin_pct=50.0,
            max_effective_leverage=28.0,
            take_profit_r=(0.5, 0.95, 1.7),
            exit_weights=(0.25, 0.35, 0.40),
        )
        risk_values = (1.6, 2.2, 2.8)
        breakout_lookbacks = (18, 24, 36)
        n_lookbacks = (24, 30, 36)
        scores = (46, 50, 54)
    elif profile == "aggressive":
        risk_values = (1.0, 1.4, 1.8)
        breakout_lookbacks = (24, 36)
        n_lookbacks = (24, 30)
        scores = (50, 54, 58)
    else:
        risk_values = (0.6, 0.9, 1.2)
        breakout_lookbacks = (36, 48)
        n_lookbacks = (30, 36)
        scores = (55, 60)

    results: list[BacktestSummary] = []
    for risk, breakout_lookback, n_lookback, min_score in product(risk_values, breakout_lookbacks, n_lookbacks, scores):
        cfg_base = replace(base, risk_per_trade_pct=risk, min_score=min_score)
        cfg = HybridConfig(
            base=cfg_base,
            breakout=BreakoutConfig(base=cfg_base, breakout_lookback=breakout_lookback, volume_lookback=breakout_lookback),
            ntrend=NTrendConfig(base=cfg_base, pattern_lookback=n_lookback),
        )
        results.append(run_hybrid_backtest(candles, cfg))
    return sorted(results, key=_rank_score, reverse=True)
