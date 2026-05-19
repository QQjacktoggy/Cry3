"""Deterministic market-regime classifier for backtests and AI gating.

The classifier only uses closed candles up to the current index. It is meant
to provide a bounded schema that a future AI model can mimic or override
without directly deciding entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.gridbot.strategy.long_pullback import (
    Candle,
    StrategyConfig,
    _atr_series,
    _ema_series,
    _rsi_series,
    _vwap_series,
)

RegimeName = Literal["trend_up", "trend_down", "range", "high_volatility", "low_liquidity", "chop"]
RiskMode = Literal["off", "small", "normal", "aggressive"]
StrategyName = Literal["orb_long", "orb_short", "vwap_reversion"]


@dataclass(frozen=True)
class RegimeFeatures:
    price: float
    atr_pct: float
    atr_percentile: float
    volume_ratio: float
    ema_fast: float | None
    ema_slow: float | None
    vwap: float | None
    rsi: float | None
    trend_slope_atr: float
    close_position_lookback: float


@dataclass(frozen=True)
class RegimeDecision:
    regime: RegimeName
    confidence: float
    risk_mode: RiskMode
    allowed_strategies: tuple[StrategyName, ...]
    features: RegimeFeatures
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RegimeContext:
    atr_values: list[float | None]
    ema_fast_values: list[float | None]
    ema_slow_values: list[float | None]
    vwap_values: list[float | None]
    rsi_values: list[float | None]
    avg_volume_values: list[float | None]
    atr_percentile_values: list[float | None]
    close_position_values: list[float | None]


def build_regime_context(
    candles: list[Candle],
    config: StrategyConfig | None = None,
    atr_percentile_lookback: int = 288,
    structure_lookback: int = 96,
    volume_lookback: int = 36,
) -> RegimeContext:
    config = config or StrategyConfig()
    closes = [candle.close for candle in candles]
    atr_values = _atr_series(candles, config.atr_period)
    return RegimeContext(
        atr_values=atr_values,
        ema_fast_values=_ema_series(closes, config.ema_fast_period),
        ema_slow_values=_ema_series(closes, config.ema_slow_period),
        vwap_values=_vwap_series(candles, config.vwap_period),
        rsi_values=_rsi_series(closes, config.rsi_period),
        avg_volume_values=_avg_volume_series(candles, volume_lookback),
        atr_percentile_values=_rolling_percentile(atr_values, atr_percentile_lookback),
        close_position_values=_rolling_close_position(candles, structure_lookback),
    )


def classify_regime(
    candles: list[Candle],
    index: int,
    context: RegimeContext | None = None,
    config: StrategyConfig | None = None,
) -> RegimeDecision | None:
    if index < 0 or index >= len(candles):
        return None
    config = config or StrategyConfig()
    context = context or build_regime_context(candles, config)
    candle = candles[index]
    atr_value = context.atr_values[index]
    avg_volume = context.avg_volume_values[index]
    atr_percentile = context.atr_percentile_values[index]
    close_position = context.close_position_values[index]
    ema_fast = context.ema_fast_values[index]
    ema_slow = context.ema_slow_values[index]
    vwap = context.vwap_values[index]
    rsi = context.rsi_values[index]
    if (
        atr_value is None
        or atr_value <= 0
        or avg_volume is None
        or avg_volume <= 0
        or atr_percentile is None
        or close_position is None
    ):
        return None

    prior_fast = context.ema_fast_values[index - 12] if index >= 12 else None
    trend_slope_atr = ((ema_fast - prior_fast) / atr_value) if ema_fast is not None and prior_fast is not None else 0.0
    volume_ratio = candle.volume / avg_volume
    atr_pct = atr_value / candle.close * 100 if candle.close else 0.0
    features = RegimeFeatures(
        price=candle.close,
        atr_pct=atr_pct,
        atr_percentile=atr_percentile,
        volume_ratio=volume_ratio,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        vwap=vwap,
        rsi=rsi,
        trend_slope_atr=trend_slope_atr,
        close_position_lookback=close_position,
    )

    reasons: list[str] = []
    if volume_ratio < 0.45:
        return RegimeDecision("low_liquidity", 0.78, "off", (), features, ("volume is too thin",))

    bullish_stack = ema_fast is not None and ema_slow is not None and ema_fast > ema_slow
    bearish_stack = ema_fast is not None and ema_slow is not None and ema_fast < ema_slow
    above_vwap = vwap is not None and candle.close >= vwap
    below_vwap = vwap is not None and candle.close < vwap
    rsi_value = rsi if rsi is not None else 50.0

    if atr_percentile >= 0.90 and volume_ratio >= 1.2:
        reasons.append("high ATR percentile with volume expansion")
        if bullish_stack and above_vwap and rsi_value >= 52:
            return RegimeDecision("high_volatility", 0.74, "normal", ("orb_long",), features, tuple(reasons))
        return RegimeDecision("high_volatility", 0.68, "small", ("orb_long",), features, tuple(reasons))

    if bullish_stack and above_vwap and trend_slope_atr >= 0.08 and close_position >= 0.55 and 48 <= rsi_value <= 76:
        confidence = min(0.92, 0.62 + min(trend_slope_atr, 1.2) * 0.12 + max(volume_ratio - 0.8, 0.0) * 0.08)
        risk_mode: RiskMode = "aggressive" if confidence >= 0.82 and volume_ratio >= 0.9 else "normal"
        reasons.append("bullish EMA/VWAP trend with constructive structure")
        return RegimeDecision("trend_up", confidence, risk_mode, ("orb_long",), features, tuple(reasons))

    if bearish_stack and below_vwap and trend_slope_atr <= -0.08 and rsi_value <= 52:
        reasons.append("bearish EMA/VWAP trend")
        return RegimeDecision("trend_down", 0.76, "off", (), features, tuple(reasons))

    if atr_percentile <= 0.25 and 0.35 <= close_position <= 0.70:
        reasons.append("compressed ATR and balanced range position")
        return RegimeDecision("range", 0.66, "small", ("vwap_reversion",), features, tuple(reasons))

    reasons.append("mixed trend and volatility signals")
    return RegimeDecision("chop", 0.62, "off", (), features, tuple(reasons))


def _avg_volume_series(candles: list[Candle], period: int) -> list[float | None]:
    values: list[float | None] = []
    for index in range(len(candles)):
        if index < period:
            values.append(None)
            continue
        window = candles[index - period:index]
        values.append(sum(candle.volume for candle in window) / period)
    return values


def _rolling_percentile(values: list[float | None], lookback: int) -> list[float | None]:
    result: list[float | None] = []
    for index, value in enumerate(values):
        if value is None or index < lookback:
            result.append(None)
            continue
        window = [item for item in values[index - lookback:index] if item is not None]
        if not window:
            result.append(None)
            continue
        below_or_equal = sum(1 for item in window if item <= value)
        result.append(below_or_equal / len(window))
    return result


def _rolling_close_position(candles: list[Candle], lookback: int) -> list[float | None]:
    result: list[float | None] = []
    for index, candle in enumerate(candles):
        if index < lookback:
            result.append(None)
            continue
        window = candles[index - lookback:index + 1]
        low = min(item.low for item in window)
        high = max(item.high for item in window)
        width = high - low
        result.append((candle.close - low) / width if width > 0 else 0.5)
    return result
