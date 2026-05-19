"""Structured market-state reviewer for strategy selection.

The reviewer is deterministic and only uses candles up to the requested index.
It is the bounded schema that a future AI model should mimic before it is
allowed to influence entries or position sizing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.gridbot.strategy.long_pullback import (
    Candle,
    StrategyConfig,
    _atr_series,
    _ema_series,
    _vwap_series,
)
from src.gridbot.strategy.regime import _avg_volume_series, _rolling_percentile


TrendState = Literal["up", "down", "range"]
Ma20Structure = Literal["above_rising", "below_falling", "flat_crossing"]
NPattern = Literal["bullish", "bearish", "none"]
BreakoutQuality = Literal["strong", "weak", "fake_risk"]
PullbackQuality = Literal["healthy", "deep", "broken", "none"]
VolatilityState = Literal["low", "normal", "high"]
Playbook = Literal["long_breakout", "long_pullback", "short_breakdown", "vwap_reversion", "no_trade"]
RiskMode = Literal["off", "small", "normal", "aggressive"]


@dataclass(frozen=True)
class MarketStateFeatures:
    price: float
    ma20: float | None
    ma20_slope_atr: float
    ema55: float | None
    vwap: float | None
    atr: float
    atr_percentile: float
    volume_ratio: float
    distance_to_ma20_atr: float
    distance_to_vwap_atr: float
    close_position_20: float
    body_to_range: float


@dataclass(frozen=True)
class MarketStateDecision:
    trend: TrendState
    ma20_structure: Ma20Structure
    n_pattern: NPattern
    breakout_quality: BreakoutQuality
    pullback_quality: PullbackQuality
    volatility: VolatilityState
    playbook: Playbook
    risk_mode: RiskMode
    confidence: float
    features: MarketStateFeatures
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class MarketStateContext:
    atr_values: list[float | None]
    ma20_values: list[float | None]
    ema55_values: list[float | None]
    vwap_values: list[float | None]
    avg_volume_values: list[float | None]
    atr_percentile_values: list[float | None]


def build_market_state_context(
    candles: list[Candle],
    config: StrategyConfig | None = None,
    atr_percentile_lookback: int = 288,
    volume_lookback: int = 36,
) -> MarketStateContext:
    config = config or StrategyConfig()
    closes = [candle.close for candle in candles]
    atr_values = _atr_series(candles, config.atr_period)
    return MarketStateContext(
        atr_values=atr_values,
        ma20_values=_ema_series(closes, 20),
        ema55_values=_ema_series(closes, config.ema_slow_period),
        vwap_values=_vwap_series(candles, config.vwap_period),
        avg_volume_values=_avg_volume_series(candles, volume_lookback),
        atr_percentile_values=_rolling_percentile(atr_values, atr_percentile_lookback),
    )


def classify_market_state(
    candles: list[Candle],
    index: int,
    context: MarketStateContext | None = None,
    config: StrategyConfig | None = None,
) -> MarketStateDecision | None:
    if index < 0 or index >= len(candles):
        return None
    config = config or StrategyConfig()
    context = context or build_market_state_context(candles, config)
    candle = candles[index]
    atr_value = context.atr_values[index]
    ma20 = context.ma20_values[index]
    ema55 = context.ema55_values[index]
    vwap = context.vwap_values[index]
    avg_volume = context.avg_volume_values[index]
    atr_percentile = context.atr_percentile_values[index]
    if (
        atr_value is None
        or atr_value <= 0
        or ma20 is None
        or avg_volume is None
        or avg_volume <= 0
        or atr_percentile is None
    ):
        return None

    prior_ma20 = context.ma20_values[index - 12] if index >= 12 else None
    ma20_slope_atr = ((ma20 - prior_ma20) / atr_value) if prior_ma20 is not None else 0.0
    volume_ratio = candle.volume / avg_volume
    distance_to_ma20_atr = (candle.close - ma20) / atr_value
    distance_to_vwap_atr = ((candle.close - vwap) / atr_value) if vwap is not None else 0.0
    close_position_20 = _close_position(candles, index, 20)
    body_to_range = abs(candle.close - candle.open) / max(candle.high - candle.low, 0.0001)
    features = MarketStateFeatures(
        price=candle.close,
        ma20=ma20,
        ma20_slope_atr=ma20_slope_atr,
        ema55=ema55,
        vwap=vwap,
        atr=atr_value,
        atr_percentile=atr_percentile,
        volume_ratio=volume_ratio,
        distance_to_ma20_atr=distance_to_ma20_atr,
        distance_to_vwap_atr=distance_to_vwap_atr,
        close_position_20=close_position_20,
        body_to_range=body_to_range,
    )

    volatility = _volatility(atr_percentile)
    ma20_structure = _ma20_structure(candle.close, ma20_slope_atr, distance_to_ma20_atr)
    trend = _trend(candle.close, ma20, ema55, ma20_slope_atr)
    n_pattern = _n_pattern(candles, index, ma20, atr_value, trend)
    breakout_quality = _breakout_quality(candles, index, atr_value, volume_ratio, close_position_20, body_to_range)
    pullback_quality = _pullback_quality(candle.close, ma20, atr_value, ma20_slope_atr, trend)
    playbook, risk_mode, confidence, reasons = _choose_playbook(
        trend,
        ma20_structure,
        n_pattern,
        breakout_quality,
        pullback_quality,
        volatility,
        features,
    )

    return MarketStateDecision(
        trend=trend,
        ma20_structure=ma20_structure,
        n_pattern=n_pattern,
        breakout_quality=breakout_quality,
        pullback_quality=pullback_quality,
        volatility=volatility,
        playbook=playbook,
        risk_mode=risk_mode,
        confidence=confidence,
        features=features,
        reasons=tuple(reasons),
    )


def _volatility(atr_percentile: float) -> VolatilityState:
    if atr_percentile >= 0.80:
        return "high"
    if atr_percentile <= 0.25:
        return "low"
    return "normal"


def _ma20_structure(price: float, slope_atr: float, distance_atr: float) -> Ma20Structure:
    if price >= 0 and distance_atr >= 0.15 and slope_atr >= 0.08:
        return "above_rising"
    if distance_atr <= -0.15 and slope_atr <= -0.08:
        return "below_falling"
    return "flat_crossing"


def _trend(price: float, ma20: float, ema55: float | None, slope_atr: float) -> TrendState:
    if ema55 is not None and price > ma20 > ema55 and slope_atr >= 0.08:
        return "up"
    if ema55 is not None and price < ma20 < ema55 and slope_atr <= -0.08:
        return "down"
    return "range"


def _n_pattern(candles: list[Candle], index: int, ma20: float, atr_value: float, trend: TrendState) -> NPattern:
    if index < 24:
        return "none"
    window = candles[index - 24:index + 1]
    closes = [candle.close for candle in window]
    first_high = max(closes[:8])
    middle_low = min(closes[8:17])
    last_close = closes[-1]
    if trend == "up" and first_high - middle_low >= atr_value * 0.6 and last_close > first_high and middle_low >= ma20 - atr_value * 0.8:
        return "bullish"
    first_low = min(closes[:8])
    middle_high = max(closes[8:17])
    if trend == "down" and middle_high - first_low >= atr_value * 0.6 and last_close < first_low and middle_high <= ma20 + atr_value * 0.8:
        return "bearish"
    return "none"


def _breakout_quality(
    candles: list[Candle],
    index: int,
    atr_value: float,
    volume_ratio: float,
    close_position: float,
    body_to_range: float,
) -> BreakoutQuality:
    if index < 20:
        return "weak"
    prior_high = max(candle.high for candle in candles[index - 20:index])
    prior_low = min(candle.low for candle in candles[index - 20:index])
    close = candles[index].close
    breaks_high = close > prior_high + atr_value * 0.05
    breaks_low = close < prior_low - atr_value * 0.05
    if (breaks_high and close_position >= 0.72 or breaks_low and close_position <= 0.28) and volume_ratio >= 1.15 and body_to_range >= 0.45:
        return "strong"
    if (breaks_high or breaks_low) and (volume_ratio < 0.85 or body_to_range < 0.25):
        return "fake_risk"
    return "weak"


def _pullback_quality(price: float, ma20: float, atr_value: float, slope_atr: float, trend: TrendState) -> PullbackQuality:
    distance = (price - ma20) / atr_value
    if trend == "up":
        if -0.45 <= distance <= 0.85 and slope_atr > 0:
            return "healthy"
        if distance < -0.9:
            return "broken"
        if distance > 1.8:
            return "deep"
    if trend == "down":
        if -0.85 <= distance <= 0.45 and slope_atr < 0:
            return "healthy"
        if distance > 0.9:
            return "broken"
        if distance < -1.8:
            return "deep"
    return "none"


def _choose_playbook(
    trend: TrendState,
    ma20_structure: Ma20Structure,
    n_pattern: NPattern,
    breakout_quality: BreakoutQuality,
    pullback_quality: PullbackQuality,
    volatility: VolatilityState,
    features: MarketStateFeatures,
) -> tuple[Playbook, RiskMode, float, list[str]]:
    reasons: list[str] = []
    if features.volume_ratio < 0.45:
        return "no_trade", "off", 0.78, ["volume too thin"]

    if trend == "up" and n_pattern == "bullish" and breakout_quality == "strong":
        reasons.append("bullish N structure with strong breakout")
        return "long_breakout", "aggressive" if volatility != "high" else "normal", 0.86, reasons
    if trend == "up" and ma20_structure == "above_rising" and pullback_quality == "healthy":
        reasons.append("healthy pullback around rising MA20")
        return "long_pullback", "normal", 0.80, reasons
    if trend == "down" and n_pattern == "bearish" and breakout_quality == "strong":
        reasons.append("bearish N structure with strong breakdown")
        return "short_breakdown", "normal", 0.82, reasons
    if volatility == "low" and abs(features.distance_to_vwap_atr) >= 1.1 and abs(features.distance_to_ma20_atr) <= 1.8:
        reasons.append("compressed market stretched away from VWAP")
        return "vwap_reversion", "small", 0.66, reasons
    if breakout_quality == "fake_risk" or abs(features.distance_to_ma20_atr) > 2.4:
        reasons.append("breakout has fakeout or extension risk")
        return "no_trade", "off", 0.72, reasons

    reasons.append("mixed structure; wait for cleaner playbook")
    return "no_trade", "off", 0.58, reasons


def _close_position(candles: list[Candle], index: int, lookback: int) -> float:
    window = candles[max(0, index + 1 - lookback):index + 1]
    low = min(candle.low for candle in window)
    high = max(candle.high for candle in window)
    width = high - low
    return (candles[index].close - low) / width if width > 0 else 0.5
