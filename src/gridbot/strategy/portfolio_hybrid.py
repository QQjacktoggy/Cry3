"""Multi-asset hybrid portfolio using breakout entries gated by N-trend structure."""

from __future__ import annotations

from dataclasses import dataclass, replace
from statistics import mean

from src.gridbot.strategy.long_breakout import BreakoutContext, _simulate_breakout
from src.gridbot.strategy.long_hybrid import (
    HybridConfig,
    generate_hybrid_signal_at,
)
from src.gridbot.strategy.long_ntrend import NTrendContext, build_ntrend_context
from src.gridbot.strategy.long_breakout import build_breakout_context
from src.gridbot.strategy.long_pullback import (
    BacktestSummary,
    Candle,
    SignalPlan,
    StrategyConfig,
    TradeResult,
    _daily_guard_reason,
    _daily_performance,
    _day_key,
    _drawdown_pct,
    _empty_daily_pnls,
    _max_consecutive_losses,
    _rank_score,
    _risk_adjusted_config,
)


@dataclass(frozen=True)
class PortfolioHybridConfig:
    symbols: tuple[str, ...] = ("ETHUSDC", "BTCUSDC", "SOLUSDC")
    base: StrategyConfig = StrategyConfig()
    per_symbol: HybridConfig = HybridConfig()
    max_concurrent_positions: int = 2
    portfolio_margin_cap_pct: float = 75.0
    benchmark_symbol: str = "BTCUSDC"
    require_benchmark_trend: bool = False
    benchmark_risk_scale: float = 0.7
    signal_weight_power: float = 1.1
    high_conviction_score: int = 90
    high_conviction_weight: float = 1.25


@dataclass(frozen=True)
class PortfolioHybridSignal:
    symbol: str
    signal: SignalPlan
    hybrid: HybridConfig


def run_portfolio_hybrid_backtest(
    candles_by_symbol: dict[str, list[Candle]],
    config: PortfolioHybridConfig | None = None,
) -> BacktestSummary:
    config = config or PortfolioHybridConfig()
    symbols = tuple(symbol for symbol in config.symbols if symbol in candles_by_symbol)
    if not symbols:
        raise ValueError("No matching symbols found for portfolio hybrid backtest.")

    base = config.base
    contexts = _build_symbol_contexts(candles_by_symbol, symbols, config)
    benchmark_symbol = config.benchmark_symbol if config.benchmark_symbol in contexts else symbols[0]
    benchmark_candles = candles_by_symbol[benchmark_symbol]
    benchmark_breakout_context = contexts[benchmark_symbol][1]
    warmup = max(_symbol_warmup(contexts[symbol][0]) for symbol in symbols) + 2

    trades: list[TradeResult] = []
    equity = base.equity_usdc
    peak_equity = equity
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    daily = _empty_daily_pnls(benchmark_candles)
    consecutive_losses = 0
    cooldown = 0
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

        runtime_base = _risk_adjusted_config(equity_base, day_pnl)
        benchmark_ok = _benchmark_allows(benchmark_candles, index, benchmark_breakout_context)
        if config.require_benchmark_trend and not benchmark_ok:
            index += 1
            continue
        runtime_base = _benchmark_adjusted_config(runtime_base, benchmark_ok, config)

        candidates: list[PortfolioHybridSignal] = []
        for symbol in symbols:
            hybrid, breakout_context, ntrend_context = contexts[symbol]
            runtime_hybrid = _runtime_hybrid(hybrid, runtime_base, symbol)
            signal = generate_hybrid_signal_at(
                candles_by_symbol[symbol],
                index,
                runtime_hybrid,
                breakout_context,
                ntrend_context,
            )
            if signal.action == "PLAN_LONG":
                candidates.append(PortfolioHybridSignal(symbol=symbol, signal=signal, hybrid=runtime_hybrid))

        if not candidates:
            index += 1
            continue

        picks = sorted(candidates, key=lambda item: _signal_priority(item, config), reverse=True)[
            : config.max_concurrent_positions
        ]
        scaled_picks = _allocate_portfolio_margin(picks, runtime_base, config)
        if not scaled_picks:
            index += 1
            continue

        next_indices: list[int] = []
        loss_seen = False
        for pick in scaled_picks:
            trade, next_index = _simulate_breakout(
                candles_by_symbol[pick.symbol],
                index + 1,
                pick.signal,
                pick.hybrid.breakout,
            )
            next_indices.append(next_index)
            if trade is None:
                continue
            trades.append(trade)
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


def sweep_portfolio_hybrid_configs(
    candles_by_symbol: dict[str, list[Candle]],
    base: StrategyConfig | None = None,
) -> list[BacktestSummary]:
    base = base or StrategyConfig()
    results: list[BacktestSummary] = []
    for risk, concurrent_positions, margin_cap, benchmark_scale, stop_after_target in (
        (1.0, 2, 75.0, 0.70, True),
        (1.4, 2, 80.0, 0.75, True),
        (1.8, 3, 90.0, 0.80, True),
        (2.2, 3, 95.0, 0.80, False),
    ):
        cfg_base = replace(
            base,
            risk_per_trade_pct=risk,
            daily_soft_loss_pct=4.0,
            daily_max_loss_pct=8.0,
            daily_loss_risk_scale=0.55,
            daily_target_stop_pct=max(base.daily_target_stop_pct, base.daily_target_min_pct),
            stop_trading_after_daily_target=stop_after_target,
            max_position_margin_pct=35.0,
            accelerator_min_score=85,
            accelerator_risk_per_trade_pct=0.35,
            accelerator_margin_pct=8.0,
            accelerator_max_effective_leverage=30.0,
        )
        hybrid = HybridConfig(
            base=cfg_base,
            breakout=replace(HybridConfig().breakout, base=cfg_base, breakout_lookback=24, volume_lookback=24),
            ntrend=replace(HybridConfig().ntrend, base=cfg_base, pattern_lookback=24),
        )
        cfg = PortfolioHybridConfig(
            base=cfg_base,
            per_symbol=hybrid,
            max_concurrent_positions=concurrent_positions,
            portfolio_margin_cap_pct=margin_cap,
            benchmark_risk_scale=benchmark_scale,
        )
        results.append(run_portfolio_hybrid_backtest(candles_by_symbol, cfg))
    return sorted(results, key=_rank_score, reverse=True)


def _build_symbol_contexts(
    candles_by_symbol: dict[str, list[Candle]],
    symbols: tuple[str, ...],
    config: PortfolioHybridConfig,
) -> dict[str, tuple[HybridConfig, BreakoutContext, NTrendContext]]:
    contexts: dict[str, tuple[HybridConfig, BreakoutContext, NTrendContext]] = {}
    for symbol in symbols:
        hybrid = _runtime_hybrid(config.per_symbol, config.base, symbol)
        contexts[symbol] = (
            hybrid,
            build_breakout_context(candles_by_symbol[symbol], hybrid.breakout),
            build_ntrend_context(candles_by_symbol[symbol], hybrid.ntrend),
        )
    return contexts


def _runtime_hybrid(hybrid: HybridConfig, runtime_base: StrategyConfig, symbol: str) -> HybridConfig:
    runtime_symbol_base = replace(runtime_base, symbol=symbol)
    runtime_breakout = replace(hybrid.breakout, base=runtime_symbol_base)
    runtime_ntrend = replace(hybrid.ntrend, base=runtime_symbol_base)
    return replace(hybrid, base=runtime_symbol_base, breakout=runtime_breakout, ntrend=runtime_ntrend)


def _symbol_warmup(config: HybridConfig) -> int:
    return max(
        config.breakout.breakout_lookback,
        config.breakout.volume_lookback,
        config.ntrend.pattern_lookback,
        config.ntrend.ma_period + 3,
        config.base.ema_slow_period,
    )


def _benchmark_allows(candles: list[Candle], index: int, context: BreakoutContext) -> bool:
    if index < 0 or index >= len(candles):
        return False
    price = candles[index].close
    ema_fast = context.ema_fast_values[index]
    ema_slow = context.ema_slow_values[index]
    vwap = context.vwap_values[index]
    rsi = context.rsi_values[index]
    if ema_fast is None or ema_slow is None or vwap is None or rsi is None:
        return False
    return ema_fast > ema_slow and price >= vwap and rsi >= 48


def _benchmark_adjusted_config(
    runtime_base: StrategyConfig,
    benchmark_ok: bool,
    config: PortfolioHybridConfig,
) -> StrategyConfig:
    if benchmark_ok or config.require_benchmark_trend:
        return runtime_base
    scale = max(min(config.benchmark_risk_scale, 1.0), 0.0)
    if scale >= 0.999:
        return runtime_base
    return replace(
        runtime_base,
        risk_per_trade_pct=runtime_base.risk_per_trade_pct * scale,
        max_position_margin_pct=runtime_base.max_position_margin_pct * scale,
        accelerator_risk_per_trade_pct=runtime_base.accelerator_risk_per_trade_pct * scale,
        accelerator_margin_pct=runtime_base.accelerator_margin_pct * scale,
    )


def _signal_priority(item: PortfolioHybridSignal, config: PortfolioHybridConfig) -> float:
    conviction = 6.0 if item.signal.score >= config.high_conviction_score else 0.0
    return item.signal.score + conviction + (2.0 if item.signal.sizing_mode == "core+accelerator" else 0.0)


def _signal_weight(item: PortfolioHybridSignal, runtime_base: StrategyConfig, config: PortfolioHybridConfig) -> float:
    score_floor = max(runtime_base.min_score, 1)
    score_edge = max(item.signal.score - score_floor + 1, 1)
    weight = score_edge ** max(config.signal_weight_power, 0.5)
    if item.signal.score >= config.high_conviction_score:
        weight *= max(config.high_conviction_weight, 1.0)
    if item.signal.sizing_mode == "core+accelerator":
        weight *= 1.1
    return weight


def _allocate_portfolio_margin(
    picks: list[PortfolioHybridSignal],
    runtime_base: StrategyConfig,
    config: PortfolioHybridConfig,
) -> list[PortfolioHybridSignal]:
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

    allocated: list[PortfolioHybridSignal] = []
    for index, item in enumerate(picks):
        allocated_margin = allocations.get(index, 0.0)
        signal = item.signal
        if allocated_margin <= 0 or signal.planned_margin_usdc <= 0 or signal.planned_qty <= 0:
            continue
        scale = min(1.0, allocated_margin / signal.planned_margin_usdc)
        scaled_signal = replace(
            signal,
            planned_notional_usdc=signal.planned_notional_usdc * scale,
            planned_margin_usdc=allocated_margin,
            planned_qty=signal.planned_qty * scale,
            risk_amount_usdc=signal.risk_amount_usdc * scale,
        )
        allocated.append(PortfolioHybridSignal(item.symbol, scaled_signal, item.hybrid))
    return allocated


def _portfolio_summary(
    base: StrategyConfig,
    trades: list[TradeResult],
    max_drawdown_usdc: float,
    max_drawdown_pct: float,
    daily_pnls: dict[str, float],
    symbols: tuple[str, ...],
    config: PortfolioHybridConfig,
) -> BacktestSummary:
    wins = [t for t in trades if t.pnl_usdc > 0]
    losses = [t for t in trades if t.pnl_usdc < 0]
    gross_profit = sum(t.pnl_usdc for t in wins)
    gross_loss = abs(sum(t.pnl_usdc for t in losses))
    net_pnl = float(sum(t.pnl_usdc for t in trades))
    avg_daily_return_pct, min_hit_rate_pct, max_hit_rate_pct = _daily_performance(base, daily_pnls)
    return BacktestSummary(
        config=base,
        trades=trades,
        net_pnl_usdc=net_pnl,
        return_pct=(net_pnl / base.equity_usdc * 100) if base.equity_usdc else 0.0,
        max_drawdown_usdc=max_drawdown_usdc,
        max_drawdown_pct=max_drawdown_pct,
        win_rate_pct=(len(wins) / len(trades) * 100) if trades else 0.0,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else 0.0,
        expectancy_usdc=mean([t.pnl_usdc for t in trades]) if trades else 0.0,
        max_consecutive_losses=_max_consecutive_losses(trades),
        avg_daily_return_pct=avg_daily_return_pct,
        daily_target_min_hit_rate_pct=min_hit_rate_pct,
        daily_target_max_hit_rate_pct=max_hit_rate_pct,
        daily_pnls=daily_pnls,
        params={
            "strategy": "portfolio_hybrid",
            "symbols": ",".join(symbols),
            "max_concurrent_positions": config.max_concurrent_positions,
            "portfolio_margin_cap_pct": config.portfolio_margin_cap_pct,
            "benchmark_symbol": config.benchmark_symbol,
            "require_benchmark_trend": config.require_benchmark_trend,
            "benchmark_risk_scale": config.benchmark_risk_scale,
        },
    )
