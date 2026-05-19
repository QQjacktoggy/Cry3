"""Regime attribution for completed backtest trades."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from src.gridbot.strategy.long_pullback import BacktestSummary, Candle, StrategyConfig, TradeResult
from src.gridbot.strategy.regime import RegimeDecision, build_regime_context, classify_regime


@dataclass(frozen=True)
class RegimeTradeAttribution:
    entry_time_ms: int
    entry_time_iso: str
    pnl_usdc: float
    r_multiple: float
    exit_reason: str
    hold_bars: int
    regime: str
    risk_mode: str
    confidence: float
    atr_percentile: float
    volume_ratio: float
    trend_slope_atr: float
    close_position_lookback: float


def attribute_trades_by_regime(
    summary: BacktestSummary,
    benchmark_candles: list[Candle],
    config: StrategyConfig | None = None,
) -> list[RegimeTradeAttribution]:
    if not summary.trades or not benchmark_candles:
        return []
    config = config or summary.config
    context = build_regime_context(benchmark_candles, config)
    candle_times = [candle.open_time_ms for candle in benchmark_candles]
    rows: list[RegimeTradeAttribution] = []
    for trade in summary.trades:
        signal_index = _signal_index_for_trade(trade, candle_times)
        if signal_index is None:
            continue
        decision = classify_regime(benchmark_candles, signal_index, context, config)
        if decision is None:
            continue
        rows.append(_row_from_decision(trade, decision))
    return rows


def summarize_regime_attribution(rows: list[RegimeTradeAttribution]) -> list[dict]:
    buckets: dict[tuple[str, str], list[RegimeTradeAttribution]] = {}
    for row in rows:
        buckets.setdefault((row.regime, row.risk_mode), []).append(row)

    summaries: list[dict] = []
    for (regime, risk_mode), bucket in buckets.items():
        pnl_values = [row.pnl_usdc for row in bucket]
        wins = [value for value in pnl_values if value > 0]
        losses = [value for value in pnl_values if value < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        summaries.append(
            {
                "regime": regime,
                "risk_mode": risk_mode,
                "trades": len(bucket),
                "net_pnl_usdc": round(sum(pnl_values), 4),
                "win_rate_pct": round(len(wins) / len(bucket) * 100, 2) if bucket else 0.0,
                "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else "inf",
                "avg_r_multiple": round(sum(row.r_multiple for row in bucket) / len(bucket), 4),
                "avg_confidence": round(sum(row.confidence for row in bucket) / len(bucket), 4),
                "avg_atr_percentile": round(sum(row.atr_percentile for row in bucket) / len(bucket), 4),
                "avg_volume_ratio": round(sum(row.volume_ratio for row in bucket) / len(bucket), 4),
            }
        )
    return sorted(summaries, key=lambda row: row["net_pnl_usdc"], reverse=True)


def _signal_index_for_trade(trade: TradeResult, candle_times: list[int]) -> int | None:
    try:
        fill_index = candle_times.index(trade.entry_time_ms)
    except ValueError:
        return None
    signal_index = fill_index - 1
    return signal_index if signal_index >= 0 else None


def _row_from_decision(trade: TradeResult, decision: RegimeDecision) -> RegimeTradeAttribution:
    features = decision.features
    return RegimeTradeAttribution(
        entry_time_ms=trade.entry_time_ms,
        entry_time_iso=datetime.fromtimestamp(trade.entry_time_ms / 1000, tz=timezone.utc).isoformat(),
        pnl_usdc=round(trade.pnl_usdc, 4),
        r_multiple=round(trade.r_multiple, 4),
        exit_reason=trade.reason,
        hold_bars=trade.hold_bars,
        regime=decision.regime,
        risk_mode=decision.risk_mode,
        confidence=round(decision.confidence, 4),
        atr_percentile=round(features.atr_percentile, 4),
        volume_ratio=round(features.volume_ratio, 4),
        trend_slope_atr=round(features.trend_slope_atr, 4),
        close_position_lookback=round(features.close_position_lookback, 4),
    )
