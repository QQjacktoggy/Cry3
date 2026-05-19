"""Multi-asset long-only open-range breakout portfolio backtest."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from src.gridbot.strategy.long_breakout import _simulate_breakout
from src.gridbot.strategy.long_orb import (
    OrbConfig,
    OrbContext,
    build_orb_context,
    generate_orb_signal_at,
    generate_orb_short_signal_at,
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
    _daily_performance,
    _drawdown_pct,
    _empty_daily_pnls,
    _max_consecutive_losses,
    _rank_score,
    _risk_adjusted_config,
)
from src.gridbot.strategy.regime import (
    RegimeContext,
    build_regime_context,
    classify_regime,
)


@dataclass(frozen=True)
class PortfolioOrbConfig:
    symbols: tuple[str, ...] = ("ETHUSDC", "BTCUSDC", "SOLUSDC")
    base: StrategyConfig = StrategyConfig()
    per_symbol: OrbConfig = OrbConfig()
    max_concurrent_positions: int = 2
    portfolio_margin_cap_pct: float = 80.0
    benchmark_symbol: str = "BTCUSDC"
    require_benchmark_trend: bool = False
    benchmark_risk_scale: float = 0.75
    soft_regime_floor: int = 0
    hard_regime_floor: int = 0
    weak_regime_max_positions: int = 1
    previous_loss_risk_scale: float = 1.0
    previous_loss_max_positions: int = 3
    allow_short: bool = False
    short_risk_scale: float = 0.75
    short_regime_max_score: int = 2
    allow_reversion: bool = False
    reversion_risk_scale: float = 0.45
    reversion_regime_max_score: int = 3
    reversion_min_deviation_atr: float = 1.2
    reversion_min_wick_ratio: float = 0.35
    reversion_max_trades_per_day: int = 1
    selector_enabled: bool = False
    selector_min_score: int = 0
    selector_strong_score: int = 7
    selector_strong_risk_scale: float = 1.0
    selector_min_orb_width_atr: float = 0.35
    selector_max_orb_width_atr: float = 6.0
    rolling_loss_lookback_days: int = 0
    rolling_loss_pause_pct: float = 0.0
    signal_weight_power: float = 1.25
    high_conviction_score: int = 88
    high_conviction_weight: float = 1.40
    ai_regime_enabled: bool = False
    ai_regime_block_enabled: bool = False
    ai_regime_block_regimes: tuple[str, ...] = ()
    ai_regime_min_confidence: float = 0.60
    ai_regime_small_risk_scale: float = 0.45
    ai_regime_aggressive_risk_scale: float = 1.20


@dataclass(frozen=True)
class PortfolioSignal:
    symbol: str
    signal: SignalPlan
    orb: OrbConfig
    kind: str = "orb"


def run_portfolio_orb_backtest(
    candles_by_symbol: dict[str, list[Candle]],
    config: PortfolioOrbConfig | None = None,
) -> BacktestSummary:
    config = config or PortfolioOrbConfig()
    symbols = tuple(symbol for symbol in config.symbols if symbol in candles_by_symbol)
    if not symbols:
        raise ValueError("No matching symbols found for portfolio ORB backtest.")

    base = config.base
    contexts = _build_symbol_contexts(candles_by_symbol, symbols, config)
    benchmark_symbol = config.benchmark_symbol if config.benchmark_symbol in contexts else symbols[0]
    benchmark_candles = candles_by_symbol[benchmark_symbol]
    benchmark_context = contexts[benchmark_symbol][1]
    regime_context = build_regime_context(benchmark_candles, base) if config.ai_regime_enabled else None
    warmup = max(_symbol_warmup(contexts[symbol][0]) for symbol in symbols) + 2

    trades: list[TradeResult] = []
    equity = base.equity_usdc
    peak_equity = equity
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    daily = _empty_daily_pnls(benchmark_candles)
    consecutive_losses = 0
    cooldown = 0
    daily_reversion_counts: dict[str, int] = {}
    index = warmup

    while index < len(benchmark_candles) - 2:
        if cooldown > 0:
            cooldown -= 1
            index += 1
            continue

        equity_base = replace(base, equity_usdc=equity) if base.compounding_enabled else base
        day = _day_key(benchmark_candles[index].open_time_ms)
        day_pnl = daily.get(day, 0.0)
        if _daily_guard_reason(equity_base, day_pnl):
            index += 1
            continue
        if _rolling_loss_exceeded(daily, day, equity_base, config):
            index += 1
            continue

        runtime_base = _risk_adjusted_config(equity_base, day_pnl)
        previous_loss_mode = _previous_day_pnl(daily, day) < 0
        runtime_base = _previous_loss_adjusted_config(runtime_base, previous_loss_mode, config)
        regime_score = _benchmark_regime_score(benchmark_candles, index, benchmark_context)
        selector_score = _selector_score(benchmark_candles, index, benchmark_context, config)
        if config.selector_enabled and selector_score < config.selector_min_score:
            index += 1
            continue
        benchmark_ok = regime_score >= max(config.hard_regime_floor, 1)
        if config.require_benchmark_trend and not benchmark_ok:
            index += 1
            continue
        if config.hard_regime_floor > 0 and regime_score < config.hard_regime_floor:
            index += 1
            continue
        runtime_base = _benchmark_adjusted_config(runtime_base, regime_score, config)
        runtime_base = _selector_adjusted_config(runtime_base, selector_score, config)
        regime_decision = _regime_decision(benchmark_candles, index, regime_context, runtime_base, config)
        if regime_decision is not None:
            if regime_decision.confidence < config.ai_regime_min_confidence:
                index += 1
                continue
            if (
                config.ai_regime_block_enabled
                and ("orb_long" not in regime_decision.allowed_strategies or regime_decision.risk_mode == "off")
            ):
                index += 1
                continue
            if regime_decision.regime in config.ai_regime_block_regimes:
                index += 1
                continue
            runtime_base = _regime_adjusted_config(runtime_base, regime_decision.risk_mode, config)

        candidates: list[PortfolioSignal] = []
        for symbol in symbols:
            orb, context = contexts[symbol]
            runtime_orb = replace(orb, base=replace(runtime_base, symbol=symbol))
            has_directional_candidate = False
            signal = generate_orb_signal_at(candles_by_symbol[symbol], index, runtime_orb, context)
            if signal.action == "PLAN_LONG":
                candidates.append(PortfolioSignal(symbol=symbol, signal=signal, orb=runtime_orb))
                has_directional_candidate = True
            if config.allow_short and regime_score <= config.short_regime_max_score:
                short_base = replace(
                    runtime_base,
                    risk_per_trade_pct=runtime_base.risk_per_trade_pct * config.short_risk_scale,
                    accelerator_risk_per_trade_pct=runtime_base.accelerator_risk_per_trade_pct * config.short_risk_scale,
                    symbol=symbol,
                )
                short_orb = replace(orb, base=short_base)
                short_signal = generate_orb_short_signal_at(candles_by_symbol[symbol], index, short_orb, context)
                if short_signal.action == "PLAN_SHORT":
                    candidates.append(PortfolioSignal(symbol=symbol, signal=short_signal, orb=short_orb))
                    has_directional_candidate = True
            if (
                config.allow_reversion
                and not has_directional_candidate
                and regime_score <= config.reversion_regime_max_score
                and daily_reversion_counts.get(day, 0) < config.reversion_max_trades_per_day
            ):
                reversion_base = replace(
                    runtime_base,
                    risk_per_trade_pct=runtime_base.risk_per_trade_pct * config.reversion_risk_scale,
                    accelerator_risk_per_trade_pct=runtime_base.accelerator_risk_per_trade_pct * config.reversion_risk_scale,
                    symbol=symbol,
                )
                reversion_orb = replace(orb, base=reversion_base)
                reversion_long = generate_vwap_reversion_long_signal_at(
                    candles_by_symbol[symbol],
                    index,
                    reversion_orb,
                    context,
                    min_deviation_atr=config.reversion_min_deviation_atr,
                    min_wick_ratio=config.reversion_min_wick_ratio,
                )
                if reversion_long.action == "PLAN_LONG":
                    candidates.append(PortfolioSignal(symbol=symbol, signal=reversion_long, orb=reversion_orb, kind="reversion"))
                    continue
                reversion_short = generate_vwap_reversion_short_signal_at(
                    candles_by_symbol[symbol],
                    index,
                    reversion_orb,
                    context,
                    min_deviation_atr=config.reversion_min_deviation_atr,
                    min_wick_ratio=config.reversion_min_wick_ratio,
                )
                if reversion_short.action == "PLAN_SHORT":
                    candidates.append(PortfolioSignal(symbol=symbol, signal=reversion_short, orb=reversion_orb, kind="reversion"))

        if not candidates:
            index += 1
            continue

        max_positions = _effective_max_positions(config, regime_score, previous_loss_mode)
        picks = sorted(candidates, key=lambda item: _signal_priority(item, config), reverse=True)[:max_positions]
        scaled_picks = _allocate_portfolio_margin(picks, runtime_base, config)
        if not scaled_picks:
            index += 1
            continue

        next_indices: list[int] = []
        loss_seen = False
        for pick in scaled_picks:
            if pick.signal.action == "PLAN_SHORT":
                trade, next_index = simulate_orb_short(
                    candles_by_symbol[pick.symbol],
                    index + 1,
                    pick.signal,
                    pick.orb,
                )
            else:
                trade, next_index = _simulate_breakout(
                    candles_by_symbol[pick.symbol],
                    index + 1,
                    pick.signal,
                    _to_breakout_proxy(pick.orb),
                )
            next_indices.append(next_index)
            if trade is None:
                continue
            trades.append(trade)
            if pick.kind == "reversion":
                daily_reversion_counts[day] = daily_reversion_counts.get(day, 0) + 1
            exit_day = _day_key(trade.exit_time_ms)
            daily[exit_day] = daily.get(exit_day, 0.0) + trade.pnl_usdc
            equity += trade.pnl_usdc
            peak_equity = max(peak_equity, equity)
            max_drawdown = min(max_drawdown, equity - peak_equity)
            max_drawdown_pct = min(max_drawdown_pct, _drawdown_pct(equity, peak_equity))
            loss_seen = loss_seen or trade.pnl_usdc < 0

        cooldown = max(cooldown, runtime_base.cooldown_bars)
        if loss_seen:
            consecutive_losses += 1
            if (
                runtime_base.max_consecutive_losses_before_cooldown > 0
                and consecutive_losses >= runtime_base.max_consecutive_losses_before_cooldown
            ):
                cooldown = max(cooldown, runtime_base.consecutive_loss_cooldown_bars)
                consecutive_losses = 0
        elif scaled_picks:
            consecutive_losses = 0

        index = max(max(next_indices, default=index + 1), index + 1)

    return _portfolio_summary(base, trades, max_drawdown, max_drawdown_pct, daily, symbols, config)


def sweep_portfolio_orb_configs(
    candles_by_symbol: dict[str, list[Candle]],
    base: StrategyConfig | None = None,
) -> list[BacktestSummary]:
    base = base or StrategyConfig()
    risk_values = (2.4, 3.0, 3.6)
    max_positions = (2, 3)
    margin_caps = (80.0, 95.0, 100.0)
    benchmark_scales = (0.7, 0.85, 1.0)
    conviction_weights = (1.25, 1.4, 1.6)
    opening_range_bars = (6, 9, 12)
    volume_ratios = (0.8, 0.95, 1.1)

    results: list[BacktestSummary] = []
    soft_floors = (0, 2)
    hard_floors = (0, 2)
    weak_max_positions = (1, 2)

    for risk in risk_values:
        for max_concurrent_positions in max_positions:
            for margin_cap in margin_caps:
                for benchmark_scale in benchmark_scales:
                    for conviction_weight in conviction_weights:
                        for orb_bars in opening_range_bars:
                            for vol_ratio in volume_ratios:
                                for soft_floor in soft_floors:
                                    for hard_floor in hard_floors:
                                        for weak_max in weak_max_positions:
                                            if hard_floor > 0 and soft_floor > 0 and hard_floor > soft_floor:
                                                continue
                                cfg = PortfolioOrbConfig(
                                    base=replace(
                                        base,
                                        risk_per_trade_pct=risk,
                                        max_effective_leverage=35.0,
                                        daily_soft_loss_pct=4.5,
                                        daily_max_loss_pct=10.0,
                                        daily_loss_risk_scale=0.65,
                                        daily_target_stop_pct=max(base.daily_target_stop_pct, base.daily_target_min_pct),
                                        max_position_margin_pct=60.0,
                                        cooldown_bars=6,
                                        max_consecutive_losses_before_cooldown=3,
                                        consecutive_loss_cooldown_bars=18,
                                        take_profit_r=(0.55, 1.1, 2.2),
                                        exit_weights=(0.25, 0.35, 0.40),
                                        min_score=44,
                                        max_holding_bars=48,
                                    ),
                                    per_symbol=OrbConfig(
                                        base=replace(base, risk_per_trade_pct=risk, min_score=44, max_holding_bars=48),
                                        opening_range_bars=orb_bars,
                                        min_volume_ratio=vol_ratio,
                                        stop_atr=0.6,
                                    ),
                                    max_concurrent_positions=max_concurrent_positions,
                                    portfolio_margin_cap_pct=margin_cap,
                                    require_benchmark_trend=False,
                                    benchmark_risk_scale=benchmark_scale,
                                    soft_regime_floor=soft_floor,
                                    hard_regime_floor=hard_floor,
                                    weak_regime_max_positions=weak_max,
                                    previous_loss_risk_scale=1.0,
                                    previous_loss_max_positions=max_concurrent_positions,
                                    allow_short=False,
                                    short_risk_scale=0.75,
                                    short_regime_max_score=2,
                                    allow_reversion=False,
                                    reversion_risk_scale=0.45,
                                    reversion_regime_max_score=3,
                                    reversion_min_deviation_atr=1.2,
                                    reversion_min_wick_ratio=0.35,
                                    reversion_max_trades_per_day=1,
                                    selector_enabled=False,
                                    selector_min_score=0,
                                    selector_strong_score=7,
                                    selector_strong_risk_scale=1.0,
                                    selector_min_orb_width_atr=0.35,
                                    selector_max_orb_width_atr=6.0,
                                    rolling_loss_lookback_days=0,
                                    rolling_loss_pause_pct=0.0,
                                    high_conviction_weight=conviction_weight,
                                )
                                results.append(run_portfolio_orb_backtest(candles_by_symbol, cfg))
    return sorted(results, key=_rank_score, reverse=True)


def _build_symbol_contexts(
    candles_by_symbol: dict[str, list[Candle]],
    symbols: tuple[str, ...],
    config: PortfolioOrbConfig,
) -> dict[str, tuple[OrbConfig, OrbContext]]:
    contexts: dict[str, tuple[OrbConfig, OrbContext]] = {}
    for symbol in symbols:
        orb = replace(config.per_symbol, base=replace(config.base, symbol=symbol))
        contexts[symbol] = (orb, build_orb_context(candles_by_symbol[symbol], orb))
    return contexts


def _symbol_warmup(config: OrbConfig) -> int:
    base = config.base
    return max(config.volume_lookback, base.ema_slow_period, base.vwap_period, config.opening_range_bars)


def _benchmark_regime_score(candles: list[Candle], index: int, context: OrbContext) -> int:
    if index < 0 or index >= len(candles):
        return 0
    price = candles[index].close
    ema_fast = context.ema_fast_values[index]
    ema_slow = context.ema_slow_values[index]
    vwap = context.vwap_values[index]
    rsi = context.rsi_values[index]
    if ema_fast is None or ema_slow is None or vwap is None or rsi is None:
        return 0
    previous_fast = context.ema_fast_values[index - 1] if index > 0 else None
    score = 0
    if ema_fast > ema_slow:
        score += 1
    if price >= vwap:
        score += 1
    if rsi >= 52:
        score += 1
    if price >= ema_fast:
        score += 1
    if previous_fast is not None and ema_fast >= previous_fast:
        score += 1
    return score


def _benchmark_adjusted_config(
    runtime_base: StrategyConfig,
    regime_score: int,
    config: PortfolioOrbConfig,
) -> StrategyConfig:
    if config.soft_regime_floor <= 0 or regime_score >= config.soft_regime_floor:
        return runtime_base
    return replace(
        runtime_base,
        risk_per_trade_pct=runtime_base.risk_per_trade_pct * config.benchmark_risk_scale,
        accelerator_risk_per_trade_pct=runtime_base.accelerator_risk_per_trade_pct * config.benchmark_risk_scale,
    )


def _selector_score(
    candles: list[Candle],
    index: int,
    context: OrbContext,
    config: PortfolioOrbConfig,
) -> int:
    if index < 0 or index >= len(candles):
        return 0
    price = candles[index].close
    atr = context.atr_values[index]
    ema_fast = context.ema_fast_values[index]
    ema_slow = context.ema_slow_values[index]
    vwap = context.vwap_values[index]
    rsi = context.rsi_values[index]
    orb_high = context.opening_range_high_values[index] if context.opening_range_high_values else None
    orb_width_atr = (
        context.opening_range_width_atr_values[index]
        if context.opening_range_width_atr_values
        else None
    )
    session_bar = context.session_bar_values[index] if context.session_bar_values else -1
    if (
        atr is None
        or atr <= 0
        or ema_fast is None
        or ema_slow is None
        or vwap is None
        or rsi is None
        or session_bar < config.per_symbol.opening_range_bars
    ):
        return 0

    score = 0
    previous_fast = context.ema_fast_values[index - 1] if index > 0 else None
    previous_close = candles[index - 1].close if index > 0 else price
    if ema_fast > ema_slow:
        score += 1
    if price >= vwap:
        score += 1
    if rsi >= 52:
        score += 1
    if price >= ema_fast:
        score += 1
    if previous_fast is not None and ema_fast >= previous_fast:
        score += 1
    if orb_high is not None and price > orb_high:
        score += 2
    if (
        orb_width_atr is not None
        and config.selector_min_orb_width_atr <= orb_width_atr <= config.selector_max_orb_width_atr
    ):
        score += 1
    if price > previous_close:
        score += 1
    if (ema_fast - ema_slow) / atr >= 0.25:
        score += 1
    return score


def _selector_adjusted_config(
    runtime_base: StrategyConfig,
    selector_score: int,
    config: PortfolioOrbConfig,
) -> StrategyConfig:
    if (
        not config.selector_enabled
        or config.selector_strong_risk_scale <= 1.0
        or selector_score < config.selector_strong_score
    ):
        return runtime_base
    scale = min(config.selector_strong_risk_scale, 2.0)
    return replace(
        runtime_base,
        risk_per_trade_pct=runtime_base.risk_per_trade_pct * scale,
        accelerator_risk_per_trade_pct=runtime_base.accelerator_risk_per_trade_pct * scale,
    )


def _previous_loss_adjusted_config(
    runtime_base: StrategyConfig,
    previous_loss_mode: bool,
    config: PortfolioOrbConfig,
) -> StrategyConfig:
    if not previous_loss_mode or config.previous_loss_risk_scale >= 1.0:
        return runtime_base
    scale = max(config.previous_loss_risk_scale, 0.05)
    return replace(
        runtime_base,
        risk_per_trade_pct=runtime_base.risk_per_trade_pct * scale,
        accelerator_risk_per_trade_pct=runtime_base.accelerator_risk_per_trade_pct * scale,
    )


def _effective_max_positions(config: PortfolioOrbConfig, regime_score: int, previous_loss_mode: bool) -> int:
    max_positions = config.max_concurrent_positions
    if config.soft_regime_floor > 0 and regime_score < config.soft_regime_floor:
        max_positions = max(1, min(max_positions, config.weak_regime_max_positions))
    if previous_loss_mode:
        max_positions = max(1, min(max_positions, config.previous_loss_max_positions))
    return max_positions


def _previous_day_pnl(daily: dict[str, float], day: str) -> float:
    previous = datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)
    return daily.get(previous.strftime("%Y-%m-%d"), 0.0)


def _rolling_loss_exceeded(
    daily: dict[str, float],
    day: str,
    equity_base: StrategyConfig,
    config: PortfolioOrbConfig,
) -> bool:
    if config.rolling_loss_lookback_days <= 0 or config.rolling_loss_pause_pct <= 0:
        return False
    cursor = datetime.strptime(day, "%Y-%m-%d")
    rolling_pnl = 0.0
    for offset in range(1, config.rolling_loss_lookback_days + 1):
        previous = cursor - timedelta(days=offset)
        rolling_pnl += daily.get(previous.strftime("%Y-%m-%d"), 0.0)
    return rolling_pnl <= -(equity_base.equity_usdc * config.rolling_loss_pause_pct / 100)


def _signal_priority(item: PortfolioSignal, config: PortfolioOrbConfig) -> float:
    priority = item.signal.score
    if item.signal.score >= config.high_conviction_score:
        priority *= config.high_conviction_weight
    return priority


def _signal_weight(item: PortfolioSignal, runtime_base: StrategyConfig, config: PortfolioOrbConfig) -> float:
    score_weight = max(item.signal.score / 100, 0.1) ** config.signal_weight_power
    leverage_weight = max(item.signal.leverage_cap / max(runtime_base.max_effective_leverage, 1), 0.35)
    conviction = config.high_conviction_weight if item.signal.score >= config.high_conviction_score else 1.0
    return score_weight * leverage_weight * conviction


def _allocate_portfolio_margin(
    picks: list[PortfolioSignal],
    runtime_base: StrategyConfig,
    config: PortfolioOrbConfig,
) -> list[PortfolioSignal]:
    margin_cap = runtime_base.equity_usdc * config.portfolio_margin_cap_pct / 100
    total_requested = sum(item.signal.planned_margin_usdc for item in picks)
    if total_requested <= 0 or margin_cap <= 0 or not picks:
        return []

    if total_requested <= margin_cap:
        allocations = {index: item.signal.planned_margin_usdc for index, item in enumerate(picks)}
    else:
        remaining = {
            index: {
                "requested": item.signal.planned_margin_usdc,
                "weight": max(_signal_weight(item, runtime_base, config), 0.01),
            }
            for index, item in enumerate(picks)
            if item.signal.planned_margin_usdc > 0
        }
        allocations = {index: 0.0 for index in remaining}
        remaining_cap = margin_cap

        while remaining and remaining_cap > 1e-9:
            total_weight = sum(entry["weight"] for entry in remaining.values())
            if total_weight <= 0:
                break
            progressed = False
            next_remaining: dict[int, dict[str, float]] = {}
            for index, entry in remaining.items():
                share = remaining_cap * entry["weight"] / total_weight
                fill = min(entry["requested"], share)
                if fill > 0:
                    allocations[index] += fill
                    remaining_cap -= fill
                    progressed = True
                leftover = entry["requested"] - fill
                if leftover > 1e-9:
                    next_remaining[index] = {"requested": leftover, "weight": entry["weight"]}
            if not progressed:
                break
            remaining = next_remaining

    allocated: list[PortfolioSignal] = []
    for index, item in enumerate(picks):
        signal = item.signal
        if signal.planned_margin_usdc <= 0 or signal.planned_qty <= 0:
            continue
        allocated_margin = allocations.get(index, 0.0)
        if allocated_margin <= 0:
            continue
        scale = min(1.0, allocated_margin / signal.planned_margin_usdc)
        scaled_signal = replace(
            signal,
            planned_notional_usdc=signal.planned_notional_usdc * scale,
            planned_margin_usdc=allocated_margin,
            planned_qty=signal.planned_qty * scale,
            risk_amount_usdc=signal.risk_amount_usdc * scale,
        )
        allocated.append(replace(item, signal=scaled_signal))
    return allocated


def _portfolio_summary(
    base: StrategyConfig,
    trades: list[TradeResult],
    max_drawdown: float,
    max_drawdown_pct: float,
    daily: dict[str, float],
    symbols: tuple[str, ...],
    config: PortfolioOrbConfig,
) -> BacktestSummary:
    avg_daily_return_pct, min_hit_rate_pct, max_hit_rate_pct = _daily_performance(base, daily)
    net_pnl = float(sum(trade.pnl_usdc for trade in trades))
    gross_profit = sum(max(trade.pnl_usdc, 0.0) for trade in trades)
    gross_loss = sum(min(trade.pnl_usdc, 0.0) for trade in trades)
    return BacktestSummary(
        config=base,
        trades=trades,
        net_pnl_usdc=net_pnl,
        return_pct=(net_pnl / base.equity_usdc * 100) if base.equity_usdc else 0.0,
        max_drawdown_usdc=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        win_rate_pct=(sum(1 for trade in trades if trade.pnl_usdc > 0) / len(trades) * 100) if trades else 0.0,
        profit_factor=(gross_profit / abs(gross_loss)) if gross_loss < 0 else float("inf"),
        expectancy_usdc=(sum(trade.pnl_usdc for trade in trades) / len(trades)) if trades else 0.0,
        max_consecutive_losses=_max_consecutive_losses(trades),
        avg_daily_return_pct=avg_daily_return_pct,
        daily_target_min_hit_rate_pct=min_hit_rate_pct,
        daily_target_max_hit_rate_pct=max_hit_rate_pct,
        daily_pnls=daily,
        params={
            "strategy": "portfolio_orb",
            "symbols": ",".join(symbols),
            "max_concurrent_positions": config.max_concurrent_positions,
            "portfolio_margin_cap_pct": config.portfolio_margin_cap_pct,
            "benchmark_risk_scale": config.benchmark_risk_scale,
            "soft_regime_floor": config.soft_regime_floor,
            "hard_regime_floor": config.hard_regime_floor,
            "weak_regime_max_positions": config.weak_regime_max_positions,
            "previous_loss_risk_scale": config.previous_loss_risk_scale,
            "previous_loss_max_positions": config.previous_loss_max_positions,
            "allow_short": config.allow_short,
            "short_risk_scale": config.short_risk_scale,
            "short_regime_max_score": config.short_regime_max_score,
            "allow_reversion": config.allow_reversion,
            "reversion_risk_scale": config.reversion_risk_scale,
            "reversion_regime_max_score": config.reversion_regime_max_score,
            "reversion_min_deviation_atr": config.reversion_min_deviation_atr,
            "reversion_min_wick_ratio": config.reversion_min_wick_ratio,
            "reversion_max_trades_per_day": config.reversion_max_trades_per_day,
            "selector_enabled": config.selector_enabled,
            "selector_min_score": config.selector_min_score,
            "selector_strong_score": config.selector_strong_score,
            "selector_strong_risk_scale": config.selector_strong_risk_scale,
            "selector_min_orb_width_atr": config.selector_min_orb_width_atr,
            "selector_max_orb_width_atr": config.selector_max_orb_width_atr,
            "rolling_loss_lookback_days": config.rolling_loss_lookback_days,
            "rolling_loss_pause_pct": config.rolling_loss_pause_pct,
            "high_conviction_weight": config.high_conviction_weight,
            "ai_regime_enabled": config.ai_regime_enabled,
            "ai_regime_block_enabled": config.ai_regime_block_enabled,
            "ai_regime_block_regimes": ",".join(config.ai_regime_block_regimes),
            "ai_regime_min_confidence": config.ai_regime_min_confidence,
            "ai_regime_small_risk_scale": config.ai_regime_small_risk_scale,
            "ai_regime_aggressive_risk_scale": config.ai_regime_aggressive_risk_scale,
            "opening_range_bars": config.per_symbol.opening_range_bars,
            "min_volume_ratio": config.per_symbol.min_volume_ratio,
        },
    )


def _to_breakout_proxy(config: OrbConfig):
    from src.gridbot.strategy.long_breakout import BreakoutConfig
    return BreakoutConfig(base=config.base, trail_atr=config.trail_atr)


def _regime_decision(
    candles: list[Candle],
    index: int,
    context: RegimeContext | None,
    runtime_base: StrategyConfig,
    config: PortfolioOrbConfig,
):
    if not config.ai_regime_enabled:
        return None
    return classify_regime(candles, index, context, runtime_base)


def _regime_adjusted_config(
    runtime_base: StrategyConfig,
    risk_mode: str,
    config: PortfolioOrbConfig,
) -> StrategyConfig:
    scale = 1.0
    if risk_mode == "small":
        scale = max(config.ai_regime_small_risk_scale, 0.05)
    elif risk_mode == "aggressive":
        scale = min(config.ai_regime_aggressive_risk_scale, 2.0)
    if abs(scale - 1.0) < 1e-9:
        return runtime_base
    return replace(
        runtime_base,
        risk_per_trade_pct=runtime_base.risk_per_trade_pct * scale,
        accelerator_risk_per_trade_pct=runtime_base.accelerator_risk_per_trade_pct * scale,
    )
