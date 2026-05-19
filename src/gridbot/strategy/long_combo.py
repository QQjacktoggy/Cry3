"""Long-only combo engine for breakout + pullback signals."""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product

from src.gridbot.strategy.long_breakout import (
    BreakoutConfig,
    build_breakout_context,
    generate_breakout_signal_at,
    _simulate_breakout,
)
from src.gridbot.strategy.long_pullback import (
    BacktestSummary,
    Candle,
    SignalPlan,
    StrategyConfig,
    TradeResult,
    build_indicator_context,
    generate_signal_at,
    _daily_guard_reason,
    _day_key,
    _drawdown_pct,
    _empty_daily_pnls,
    _rank_score,
    _risk_adjusted_config,
    _simulate_plan,
    _summary,
)


@dataclass(frozen=True)
class ComboConfig:
    base: StrategyConfig = StrategyConfig()
    breakout: BreakoutConfig = BreakoutConfig()
    breakout_score_bonus: int = 5
    pullback_score_bonus: int = 0


def generate_combo_signal(candles: list[Candle], config: ComboConfig | None = None) -> SignalPlan:
    config = config or ComboConfig()
    base = config.base
    breakout_config = replace(config.breakout, base=base)
    pullback_context = build_indicator_context(candles, base)
    breakout_context = build_breakout_context(candles, breakout_config)
    choice = _choose_signal(
        candles,
        len(candles) - 1,
        base,
        breakout_config,
        pullback_context,
        breakout_context,
        config,
    )
    if choice is None:
        return generate_signal_at(candles, len(candles) - 1, base, pullback_context)
    _, signal = choice
    return signal


def run_combo_backtest(candles: list[Candle], config: ComboConfig | None = None) -> BacktestSummary:
    config = config or ComboConfig()
    base = config.base
    breakout_config = replace(config.breakout, base=base)
    pullback_context = build_indicator_context(candles, base)
    breakout_context = build_breakout_context(candles, breakout_config)
    warmup = max(
        base.support_lookback,
        base.vwap_period,
        base.ema_slow_period,
        breakout_config.breakout_lookback,
        breakout_config.volume_lookback,
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
        runtime_breakout = replace(breakout_config, base=runtime_base)
        choice = _choose_signal(
            candles,
            index,
            runtime_base,
            runtime_breakout,
            pullback_context,
            breakout_context,
            config,
        )
        if choice is None:
            index += 1
            continue

        strategy_name, signal = choice
        if strategy_name == "breakout":
            trade, next_index = _simulate_breakout(candles, index + 1, signal, runtime_breakout)
        else:
            trade, next_index = _simulate_plan(candles, index + 1, signal, runtime_base)
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
        "strategy": "combo_breakout_pullback",
        "breakout_lookback": breakout_config.breakout_lookback,
        "breakout_stop_atr": breakout_config.stop_atr,
        "pullback_stop_atr": base.stop_atr,
        "breakout_score_bonus": config.breakout_score_bonus,
        "accelerator_min_score": base.accelerator_min_score,
        "accelerator_risk": base.accelerator_risk_per_trade_pct,
        "accelerator_margin_pct": base.accelerator_margin_pct,
        "accelerator_max_leverage": base.accelerator_max_effective_leverage,
    })


def sweep_combo_configs(
    candles: list[Candle],
    base: StrategyConfig | None = None,
    profile: str = "balanced",
) -> list[BacktestSummary]:
    base = base or StrategyConfig()
    if profile == "spec":
        base = replace(
            base,
            daily_soft_loss_pct=5.0,
            daily_max_loss_pct=12.0,
            daily_loss_risk_scale=0.65,
            daily_target_stop_pct=max(base.daily_target_stop_pct, base.daily_target_min_pct),
            max_position_margin_pct=60.0,
            max_effective_leverage=35.0,
            max_consecutive_losses_before_cooldown=3,
            consecutive_loss_cooldown_bars=18,
            cooldown_bars=4,
            take_profit_r=(0.45, 0.9, 1.8),
            exit_weights=(0.25, 0.35, 0.40),
        )
        risk_values = (3.0, 4.0, 5.0)
        breakout_lookbacks = (18, 24)
        breakout_stops = (0.55, 0.7, 0.85)
        breakout_scores = (40, 45)
        pullback_scores = (40, 45)
        pullback_stops = (0.8, 1.0)
        accelerator_sets = (
            (75, 1.0, 12.0, 40.0),
            (80, 1.4, 16.0, 45.0),
        )
    else:
        risk_values = (1.0, 1.5, 2.0) if profile == "aggressive" else (0.5, 0.8, 1.0)
        breakout_lookbacks = (24, 36, 48)
        breakout_stops = (0.9, 1.1, 1.35)
        breakout_scores = (45, 50, 55)
        pullback_scores = (45, 50, 55)
        pullback_stops = (1.0, 1.2, 1.6)
        accelerator_sets = (
            (base.accelerator_min_score, base.accelerator_risk_per_trade_pct, base.accelerator_margin_pct, base.accelerator_max_effective_leverage),
        )

    results: list[BacktestSummary] = []
    for risk, lookback, breakout_stop, breakout_score, pullback_score, pullback_stop, accelerator in product(
        risk_values,
        breakout_lookbacks,
        breakout_stops,
        breakout_scores,
        pullback_scores,
        pullback_stops,
        accelerator_sets,
    ):
        accelerator_score, accelerator_risk, accelerator_margin, accelerator_leverage = accelerator
        cfg_base = replace(
            base,
            risk_per_trade_pct=risk,
            min_score=pullback_score,
            stop_atr=pullback_stop,
            entry_spacing_atr=0.25,
            accelerator_min_score=accelerator_score,
            accelerator_risk_per_trade_pct=accelerator_risk,
            accelerator_margin_pct=accelerator_margin,
            accelerator_max_effective_leverage=accelerator_leverage,
        )
        breakout = BreakoutConfig(
            base=replace(cfg_base, min_score=breakout_score, max_holding_bars=72),
            breakout_lookback=lookback,
            volume_lookback=lookback,
            stop_atr=breakout_stop,
            min_volume_ratio=0.55,
        )
        results.append(run_combo_backtest(candles, ComboConfig(base=cfg_base, breakout=breakout)))
    return sorted(results, key=_rank_score, reverse=True)


def _choose_signal(
    candles: list[Candle],
    index: int,
    base: StrategyConfig,
    breakout: BreakoutConfig,
    pullback_context,
    breakout_context,
    combo: ComboConfig,
) -> tuple[str, SignalPlan] | None:
    candidates: list[tuple[int, str, SignalPlan]] = []
    pullback = generate_signal_at(candles, index, base, pullback_context)
    if pullback.action == "PLAN_LONG":
        candidates.append((pullback.score + combo.pullback_score_bonus, "pullback", pullback))

    breakout_signal = generate_breakout_signal_at(candles, index, breakout, breakout_context)
    if breakout_signal.action == "PLAN_LONG":
        candidates.append((breakout_signal.score + combo.breakout_score_bonus, "breakout", breakout_signal))

    if not candidates:
        return None
    _, strategy_name, signal = max(candidates, key=lambda item: item[0])
    return strategy_name, signal
