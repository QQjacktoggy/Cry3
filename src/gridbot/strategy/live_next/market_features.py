"""Causal one-second trade-flow features for offline Live Next research."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterator, Sequence

from .exact_replay import ExactAggTrade
from .features import FeatureObservation, FeatureSnapshot


FEATURE_ENGINE_VERSION = "live_next.tradeflow_features.v2"
DECISION_BIN_MS = 1_000
RESEARCH_DECISION_STRIDE_MS = 5_000
WARMUP_MS = 60_000
BPS = 10_000.0


@dataclass(frozen=True, slots=True)
class CausalFeatureFrame:
    decision_time_ms: int
    market_data_max_event_ms: int
    anchor_event_id: str
    reference_price: Decimal
    snapshot: FeatureSnapshot


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _move_bps(start: float, end: float) -> float:
    return (end / start - 1.0) * BPS if start > 0 else 0.0


def _extreme_move_and_retrace(
    base: float,
    current: float,
    highs: Sequence[float],
    lows: Sequence[float],
) -> tuple[float, float]:
    high, low = max(highs), min(lows)
    up = _move_bps(base, high)
    down = _move_bps(base, low)
    if abs(up) >= abs(down):
        denominator = high - base
        retrace = (high - current) / denominator if denominator > 0 else 0.0
        return up, _clamp(retrace)
    denominator = base - low
    retrace = (current - low) / denominator if denominator > 0 else 0.0
    return down, _clamp(retrace)


def _flow_ratio(buy: float, sell: float, *, long_side: bool) -> float:
    total = buy + sell
    if total <= 0:
        return 0.5
    return (buy if long_side else sell) / total


def iter_causal_feature_frames(
    trades: Sequence[ExactAggTrade],
    *,
    decision_bin_ms: int = DECISION_BIN_MS,
    decision_stride_ms: int = DECISION_BIN_MS,
    warmup_ms: int = WARMUP_MS,
) -> Iterator[CausalFeatureFrame]:
    """Yield features at bin end using only trades strictly before that end."""

    if not trades:
        return
    if decision_bin_ms <= 0 or warmup_ms < 60_000:
        raise ValueError("feature engine requires positive bins and at least 60s warmup")
    if decision_stride_ms < decision_bin_ms or decision_stride_ms % decision_bin_ms:
        raise ValueError("decision stride must be a positive multiple of the bin size")
    ordered = tuple(trades)
    previous_key: tuple[int, int] | None = None
    for trade in ordered:
        if not isinstance(trade, ExactAggTrade):
            raise TypeError("trade-flow features require ExactAggTrade records")
        if previous_key is not None and trade.ordering_key <= previous_key:
            raise ValueError("aggTrades must be strictly causal ordered")
        previous_key = trade.ordering_key

    start_ms = ordered[0].transact_time_ms // decision_bin_ms * decision_bin_ms
    final_index = (ordered[-1].transact_time_ms - start_ms) // decision_bin_ms
    count = final_index + 1
    close: list[float | None] = [None] * count
    high: list[float | None] = [None] * count
    low: list[float | None] = [None] * count
    buy_notional = [0.0] * count
    sell_notional = [0.0] * count
    last_id: list[int | None] = [None] * count
    last_event_ms: list[int | None] = [None] * count

    for trade in ordered:
        index = (trade.transact_time_ms - start_ms) // decision_bin_ms
        price = float(trade.price)
        notional = price * float(trade.quantity)
        close[index] = price
        high[index] = price if high[index] is None else max(float(high[index]), price)
        low[index] = price if low[index] is None else min(float(low[index]), price)
        if trade.is_buyer_maker:
            sell_notional[index] += notional
        else:
            buy_notional[index] += notional
        last_id[index] = trade.agg_trade_id
        last_event_ms[index] = trade.transact_time_ms

    previous_close: float | None = None
    for index in range(count):
        if close[index] is not None:
            previous_close = float(close[index])
        elif previous_close is not None:
            close[index] = previous_close
        if previous_close is not None:
            high[index] = previous_close if high[index] is None else high[index]
            low[index] = previous_close if low[index] is None else low[index]

    prefix_buy = [0.0]
    prefix_sell = [0.0]
    for buy, sell in zip(buy_notional, sell_notional):
        prefix_buy.append(prefix_buy[-1] + buy)
        prefix_sell.append(prefix_sell[-1] + sell)

    def flow(window_seconds: int, end_index: int) -> tuple[float, float]:
        begin = max(0, end_index + 1 - window_seconds)
        return (
            prefix_buy[end_index + 1] - prefix_buy[begin],
            prefix_sell[end_index + 1] - prefix_sell[begin],
        )

    warmup_bins = warmup_ms // decision_bin_ms
    for index in range(max(60, warmup_bins), count):
        if last_id[index] is None or close[index] is None:
            continue
        decision_time = start_ms + (index + 1) * decision_bin_ms
        if decision_time % decision_stride_ms:
            continue
        event_time = int(last_event_ms[index])
        if event_time >= decision_time:
            raise ValueError("feature bin contains a post-decision trade")
        current = float(close[index])
        p2, p3, p30 = float(close[index - 2]), float(close[index - 3]), float(close[index - 30])
        highs2 = [float(value) for value in high[index - 1 : index + 1]]
        lows2 = [float(value) for value in low[index - 1 : index + 1]]
        highs3 = [float(value) for value in high[index - 2 : index + 1]]
        lows3 = [float(value) for value in low[index - 2 : index + 1]]
        move2, retrace2 = _extreme_move_and_retrace(p2, current, highs2, lows2)
        move3, retrace3 = _extreme_move_and_retrace(p3, current, highs3, lows3)
        move30 = _move_bps(p30, current)

        window30_high = max(float(value) for value in high[index - 29 : index + 1])
        window30_low = min(float(value) for value in low[index - 29 : index + 1])
        if move30 >= 0:
            pullback_denominator = window30_high - p30
            pullback = (
                (window30_high - current) / pullback_denominator
                if pullback_denominator > 0
                else 0.0
            )
        else:
            pullback_denominator = p30 - window30_low
            pullback = (
                (current - window30_low) / pullback_denominator
                if pullback_denominator > 0
                else 0.0
            )
        pullback = _clamp(pullback)

        range_high = max(float(value) for value in high[index - 59 : index + 1])
        range_low = min(float(value) for value in low[index - 59 : index + 1])
        range_span = range_high - range_low
        range_position = (
            _clamp((current - range_low) / range_span) if range_span > 0 else 0.5
        )
        prior_high = max(float(value) for value in high[index - 59 : index - 3])
        prior_low = min(float(value) for value in low[index - 59 : index - 3])
        recent_high = max(float(value) for value in high[index - 3 : index + 1])
        recent_low = min(float(value) for value in low[index - 3 : index + 1])
        false_break = 0.0
        reclaim = 0.0
        reclaim_side: bool | None = None
        if recent_low < prior_low <= current:
            false_break = (prior_low - recent_low) / prior_low * BPS
            reclaim = (current - prior_low) / prior_low * BPS
            reclaim_side = True
        elif recent_high > prior_high >= current:
            false_break = (recent_high - prior_high) / prior_high * BPS
            reclaim = (prior_high - current) / prior_high * BPS
            reclaim_side = False

        buy3, sell3 = flow(3, index)
        buy1, sell1 = flow(1, index)
        impulse_long = move3 > 0 if move3 != 0 else move30 >= 0
        impulse_flow = _flow_ratio(buy3, sell3, long_side=impulse_long)
        trend_flow = _flow_ratio(buy3, sell3, long_side=move30 >= 0)
        range_inward_flow = _flow_ratio(
            buy1, sell1, long_side=range_position <= 0.5
        )
        shock_reversal_flow = _flow_ratio(buy1, sell1, long_side=move2 < 0)
        buy30, sell30 = flow(30, index)
        short_total = buy3 + sell3
        long_total = buy30 + sell30
        intensity = (
            (short_total / 3.0) / (long_total / 30.0)
            if short_total > 0 and long_total > 0
            else 0.0
        )
        execution_quality = _clamp(intensity / 2.0)
        range_width_bps = range_span / current * BPS if current > 0 else 0.0
        exit_economics = _clamp(
            max(abs(move2), abs(move3), abs(move30), range_width_bps / 2.0) / 30.0
        )
        shock_score = _clamp(abs(move2) / 25.0)
        trend_score = _clamp(abs(move30) / 30.0)
        range_score = _clamp(
            1.0 - max(abs(move30) / 20.0, abs(move2) / 15.0)
        )
        direction_score = max(-1.0, min(1.0, move30 / 30.0))
        values = {
            "anchor_event_id": str(last_id[index]),
            "direction_score": direction_score,
            "directional_flow_ratio": impulse_flow,
            "impulse_flow_ratio": impulse_flow,
            "range_inward_flow_ratio": range_inward_flow,
            "trend_flow_ratio": trend_flow,
            "execution_quality": execution_quality,
            "exit_economics": exit_economics,
            "false_break_bps": false_break,
            "move_2s_bps": move2,
            "move_3s_bps": move3,
            "move_30s_bps": move30,
            "pullback_fraction": pullback,
            "range_position_60s": range_position,
            "range_score": range_score,
            "reclaim_bps": reclaim,
            "retrace_fraction": retrace3 if abs(move3) >= abs(move2) else retrace2,
            "reversal_flow_ratio": shock_reversal_flow,
            "shock_reversal_flow_ratio": shock_reversal_flow,
            "shock_score": shock_score,
            "trend_score": trend_score,
        }
        observations = {
            name: FeatureObservation(
                value=value,
                event_time_ms=event_time,
                available_at_ms=decision_time,
            )
            for name, value in values.items()
        }
        snapshot = FeatureSnapshot(
            decision_time_ms=decision_time,
            features=observations,
            quality_flags=("topbook_missing",),
            feature_version=FEATURE_ENGINE_VERSION,
        )
        yield CausalFeatureFrame(
            decision_time_ms=decision_time,
            market_data_max_event_ms=event_time,
            anchor_event_id=str(last_id[index]),
            reference_price=Decimal(str(current)),
            snapshot=snapshot,
        )


__all__ = [
    "CausalFeatureFrame",
    "DECISION_BIN_MS",
    "FEATURE_ENGINE_VERSION",
    "RESEARCH_DECISION_STRIDE_MS",
    "WARMUP_MS",
    "iter_causal_feature_frames",
]
