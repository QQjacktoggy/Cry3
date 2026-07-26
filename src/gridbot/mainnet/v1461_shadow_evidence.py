"""Pure aggTrade evidence helpers for v1.4.61 strategy-gate shadows.

This module deliberately owns no network, persistence, order, or position
operations.  Callers must fetch Binance ``aggTrades`` and provide explicit
coverage metadata.  A result is promotion-safe only when ``data_complete`` is
true; malformed numbers and unproven coverage always produce
``DATA_INCOMPLETE`` with ``None`` PnL rather than an invented zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Any, Iterable, Mapping, Sequence

from src.gridbot.mainnet.v1460_weak_shadow_runtime import AggTrade, normalize_agg_trades


_MAX_TIMESTAMP_MS = 9_223_372_036_854_775_807


class ShadowEvidenceOutcome(str, Enum):
    TP_FIRST = "tp1_first"
    SL_FIRST = "sl_first"
    MAX_HOLD = "max_hold"
    NO_FILL = "no_fill"
    DATA_INCOMPLETE = "data_incomplete"


@dataclass(frozen=True, slots=True)
class ShadowCostModel:
    """Fee and conservative adverse-slippage assumptions.

    ``slippage_bp`` is charged once against exit notional for every filled
    terminal path.  It is intentionally explicit even for maker TP exits so a
    caller can keep promotion evidence conservative; pass ``0`` when no extra
    buffer is desired.
    """

    maker_fee_rate: float = 0.0
    taker_fee_rate: float = 0.0004
    slippage_bp: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (
            ("maker_fee_rate", self.maker_fee_rate),
            ("taker_fee_rate", self.taker_fee_rate),
        ):
            parsed = _finite_number(name, value)
            if not 0.0 <= parsed < 1.0:
                raise ValueError(f"{name} must be in [0, 1)")
        slippage = _finite_number("slippage_bp", self.slippage_bp)
        if slippage < 0.0:
            raise ValueError("slippage_bp must be non-negative")


@dataclass(frozen=True, slots=True)
class _Sample:
    side: str
    start_ms: int
    entry_deadline_ms: int
    outcome_deadline_ms: int
    entry_price: float
    tp_price: float
    sl_price: float
    notional_usdc: float
    fill_model: str


@dataclass(frozen=True, slots=True)
class _Touch:
    outcome: ShadowEvidenceOutcome
    trade: AggTrade | None
    ambiguous_same_timestamp: bool = False


def _finite_number(name: str, value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be a finite number")
    return parsed


def _positive_number(name: str, value: Any) -> float:
    parsed = _finite_number(name, value)
    if parsed <= 0.0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _nonnegative_ms(name: str, value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if (
        not math.isfinite(numeric)
        or numeric != parsed
        or parsed < 0
        or parsed > _MAX_TIMESTAMP_MS
    ):
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def _normalize_side(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("side must be LONG/SHORT or BUY/SELL")
    side = value.strip().upper()
    side = {"BUY": "LONG", "SELL": "SHORT"}.get(side, side)
    if side not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG/SHORT or BUY/SELL")
    return side


def _sample_notional(sample: Mapping[str, Any]) -> float:
    for field in ("requested_notional_usdc", "raw_requested_notional_usdc", "notional_usdc"):
        if field in sample and sample[field] is not None:
            return _positive_number(field, sample[field])
    raise ValueError("requested_notional_usdc is required")


def _parse_sample(sample: Mapping[str, Any]) -> _Sample:
    if not isinstance(sample, Mapping):
        raise ValueError("sample must be a mapping")
    side = _normalize_side(sample.get("side"))
    start_ms = _nonnegative_ms("start_ms", sample.get("start_ms"))
    entry_ttl_s = _positive_number("entry_ttl_s", sample.get("entry_ttl_s"))
    outcome_ttl_s = _positive_number("outcome_ttl_s", sample.get("outcome_ttl_s"))
    entry_ttl_ms = entry_ttl_s * 1_000.0
    outcome_ttl_ms = outcome_ttl_s * 1_000.0
    if not math.isfinite(entry_ttl_ms) or not math.isfinite(outcome_ttl_ms):
        raise ValueError("shadow TTL is too large")
    entry_deadline_ms = start_ms + int(round(entry_ttl_ms))
    outcome_deadline_ms = start_ms + int(round(outcome_ttl_ms))
    if entry_deadline_ms > _MAX_TIMESTAMP_MS or outcome_deadline_ms > _MAX_TIMESTAMP_MS:
        raise ValueError("shadow deadline exceeds timestamp range")
    if outcome_deadline_ms < entry_deadline_ms:
        raise ValueError("outcome_ttl_s cannot be shorter than entry_ttl_s")

    entry = _positive_number("entry_price", sample.get("entry_price"))
    tp = _positive_number("tp_price", sample.get("tp_price"))
    sl = _positive_number("sl_price", sample.get("sl_price"))
    if side == "LONG" and not sl < entry < tp:
        raise ValueError("LONG requires sl_price < entry_price < tp_price")
    if side == "SHORT" and not tp < entry < sl:
        raise ValueError("SHORT requires tp_price < entry_price < sl_price")

    fill_model = str(sample.get("fill_model") or "limit_touch").strip().lower()
    if fill_model not in {"limit_touch", "immediate_shadow"}:
        raise ValueError("fill_model must be limit_touch or immediate_shadow")
    return _Sample(
        side=side,
        start_ms=start_ms,
        entry_deadline_ms=entry_deadline_ms,
        outcome_deadline_ms=outcome_deadline_ms,
        entry_price=entry,
        tp_price=tp,
        sl_price=sl,
        notional_usdc=_sample_notional(sample),
        fill_model=fill_model,
    )


def _incomplete(reason: str, *, required_deadline_ms: int | None = None) -> dict[str, Any]:
    return {
        "evidence_source": "binance_aggTrade",
        "shadow_outcome": ShadowEvidenceOutcome.DATA_INCOMPLETE.value,
        "first_touch_result": ShadowEvidenceOutcome.DATA_INCOMPLETE.name,
        "status": "data_incomplete",
        "evaluable": False,
        "data_complete": False,
        "filled": None,
        "filled_ts": None,
        "resolved_ts": None,
        "hit_time_ms": None,
        "exit_reference_price": None,
        "exit_liquidity": None,
        "paper_pnl_bp_before_fee": None,
        "paper_pnl_bp_after_fee": None,
        "paper_pnl_usdc_before_cost": None,
        "paper_pnl_usdc_after_fee": None,
        "estimated_fee_bp": None,
        "estimated_fee_usdc": None,
        "conservative_slippage_buffer_bp": None,
        "estimated_slippage_usdc": None,
        "data_quality": {
            "status": ShadowEvidenceOutcome.DATA_INCOMPLETE.name,
            "complete": False,
            "reason": reason,
            "required_deadline_ms": required_deadline_ms,
        },
    }


def _find_fill(trades: Sequence[AggTrade], sample: _Sample) -> AggTrade | None:
    if sample.fill_model == "immediate_shadow":
        return next((trade for trade in trades if trade.time_ms >= sample.start_ms), None)
    for trade in trades:
        if not sample.start_ms <= trade.time_ms <= sample.entry_deadline_ms:
            continue
        if sample.side == "LONG" and trade.price <= sample.entry_price:
            return trade
        if sample.side == "SHORT" and trade.price >= sample.entry_price:
            return trade
    return None


def _touch_at_or_after_fill(
    trades: Sequence[AggTrade],
    sample: _Sample,
    fill: AggTrade,
) -> _Touch:
    # Include the fill aggTrade itself.  A gap-through entry can therefore
    # immediately stop out instead of being hidden until the next tick.
    path = [
        trade
        for trade in trades
        if (trade.time_ms, trade.agg_trade_id) >= (fill.time_ms, fill.agg_trade_id)
        and trade.time_ms <= sample.outcome_deadline_ms
    ]
    index = 0
    while index < len(path):
        timestamp = path[index].time_ms
        group: list[AggTrade] = []
        while index < len(path) and path[index].time_ms == timestamp:
            group.append(path[index])
            index += 1
        if sample.side == "LONG":
            tp_hits = [trade for trade in group if trade.price >= sample.tp_price]
            sl_hits = [trade for trade in group if trade.price <= sample.sl_price]
        else:
            tp_hits = [trade for trade in group if trade.price <= sample.tp_price]
            sl_hits = [trade for trade in group if trade.price >= sample.sl_price]
        if sl_hits:
            # Timestamp-level ordering may be ambiguous even though aggregate
            # IDs are ordered.  Promotion evidence resolves that ambiguity in
            # the loss direction by contract.
            return _Touch(
                ShadowEvidenceOutcome.SL_FIRST,
                min(sl_hits, key=lambda trade: trade.agg_trade_id),
                ambiguous_same_timestamp=bool(tp_hits),
            )
        if tp_hits:
            return _Touch(
                ShadowEvidenceOutcome.TP_FIRST,
                min(tp_hits, key=lambda trade: trade.agg_trade_id),
            )
    return _Touch(ShadowEvidenceOutcome.MAX_HOLD, None)


def _first_gap_reason(trades: Sequence[AggTrade], start_ms: int, end_ms: int) -> str | None:
    relevant = [trade for trade in trades if start_ms <= trade.time_ms <= end_ms]
    for previous, current in zip(relevant, relevant[1:]):
        if current.agg_trade_id <= previous.agg_trade_id:
            return "agg_trade_order_invalid"
        if current.agg_trade_id != previous.agg_trade_id + 1:
            return f"agg_trade_id_gap:{previous.agg_trade_id + 1}-{current.agg_trade_id - 1}"
    return None


def _coverage_gap_reason(
    gaps: Iterable[tuple[int, int]] | None,
    start_ms: int,
    end_ms: int,
) -> str | None:
    if gaps is None:
        return None
    try:
        parsed_gaps = list(gaps)
    except TypeError:
        return "coverage_gaps_invalid"
    for gap in parsed_gaps:
        if not isinstance(gap, (tuple, list)) or len(gap) != 2:
            return "coverage_gaps_invalid"
        try:
            gap_start = _nonnegative_ms("coverage_gap_start_ms", gap[0])
            gap_end = _nonnegative_ms("coverage_gap_end_ms", gap[1])
        except ValueError:
            return "coverage_gaps_invalid"
        if gap_end < gap_start:
            return "coverage_gaps_invalid"
        if gap_start <= end_ms and gap_end >= start_ms:
            return f"coverage_time_gap:{gap_start}-{gap_end}"
    return None


def _normalize_rows(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[AggTrade, ...]:
    """Apply stricter integer validation before the shared v1.4.60 parser."""

    materialized = list(rows)
    for row in materialized:
        if not isinstance(row, Mapping):
            raise ValueError("aggTrade row must be a mapping")
        raw_id = row.get("a", row.get("id"))
        raw_time = row.get("T", row.get("time"))
        _nonnegative_ms("agg_trade_id", raw_id)
        _nonnegative_ms("agg_trade_time_ms", raw_time)
    return normalize_agg_trades(materialized)


def _path_metrics(
    trades: Sequence[AggTrade],
    sample: _Sample,
    fill: AggTrade,
    terminal_ms: int,
) -> tuple[float, float]:
    prices = [sample.entry_price]
    prices.extend(
        trade.price
        for trade in trades
        if (trade.time_ms, trade.agg_trade_id) >= (fill.time_ms, fill.agg_trade_id)
        and trade.time_ms <= terminal_ms
    )
    if sample.side == "LONG":
        favorable = [(price / sample.entry_price - 1.0) * 10_000.0 for price in prices]
        adverse = [(1.0 - price / sample.entry_price) * 10_000.0 for price in prices]
    else:
        favorable = [(1.0 - price / sample.entry_price) * 10_000.0 for price in prices]
        adverse = [(price / sample.entry_price - 1.0) * 10_000.0 for price in prices]
    return max(0.0, max(favorable)), max(0.0, max(adverse))


def _costs(
    sample: _Sample,
    *,
    exit_price: float,
    exit_liquidity: str,
    cost_model: ShadowCostModel,
) -> dict[str, float | str]:
    quantity = sample.notional_usdc / sample.entry_price
    if sample.side == "LONG":
        gross_usdc = quantity * (exit_price - sample.entry_price)
    else:
        gross_usdc = quantity * (sample.entry_price - exit_price)
    exit_notional = quantity * exit_price
    entry_fee = sample.notional_usdc * float(cost_model.maker_fee_rate)
    exit_rate = (
        float(cost_model.maker_fee_rate)
        if exit_liquidity == "MAKER"
        else float(cost_model.taker_fee_rate)
    )
    exit_fee = exit_notional * exit_rate
    total_fee = entry_fee + exit_fee
    slippage_usdc = exit_notional * float(cost_model.slippage_bp) / 10_000.0
    net_usdc = gross_usdc - total_fee - slippage_usdc
    result: dict[str, float | str] = {
        "paper_pnl_bp_before_fee": gross_usdc / sample.notional_usdc * 10_000.0,
        "paper_pnl_bp_after_fee": net_usdc / sample.notional_usdc * 10_000.0,
        "paper_pnl_usdc_before_cost": gross_usdc,
        "paper_pnl_usdc_after_fee": net_usdc,
        "entry_fee_usdc": entry_fee,
        "exit_fee_usdc": exit_fee,
        "estimated_fee_usdc": total_fee,
        "estimated_fee_bp": total_fee / sample.notional_usdc * 10_000.0,
        "estimated_slippage_usdc": slippage_usdc,
        "conservative_slippage_buffer_bp": slippage_usdc
        / sample.notional_usdc
        * 10_000.0,
        "fee_model": "maker_entry_outcome_liquidity_exit",
    }
    if any(
        not math.isfinite(value)
        for value in result.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ):
        raise ValueError("cost calculation produced a non-finite value")
    return result


def evaluate_v1461_shadow_evidence(
    sample: Mapping[str, Any],
    agg_trades: Iterable[Mapping[str, Any]] | None,
    *,
    coverage_start_ms: Any,
    coverage_end_ms: Any,
    coverage_complete: bool,
    coverage_gaps_ms: Iterable[tuple[int, int]] | None = None,
    cost_model: ShadowCostModel | None = None,
) -> dict[str, Any]:
    """Resolve one v1.4.61 sample using Binance aggTrades only.

    ``coverage_start_ms``/``coverage_end_ms`` describe pagination coverage,
    not merely the timestamps of the first and last returned rows.  An empty
    trade list can therefore prove ``NO_FILL`` only when that coverage is
    explicitly complete.  The function never mutates ``sample`` or the rows.
    """

    try:
        parsed_sample = _parse_sample(sample)
    except ValueError as exc:
        return _incomplete(f"invalid_sample:{exc}")
    required_deadline_ms = parsed_sample.entry_deadline_ms
    if coverage_complete is not True:
        return _incomplete("coverage_not_complete", required_deadline_ms=required_deadline_ms)
    try:
        coverage_start = _nonnegative_ms("coverage_start_ms", coverage_start_ms)
        coverage_end = _nonnegative_ms("coverage_end_ms", coverage_end_ms)
    except ValueError as exc:
        return _incomplete(f"invalid_coverage:{exc}", required_deadline_ms=required_deadline_ms)
    if coverage_end < coverage_start:
        return _incomplete("coverage_end_before_start", required_deadline_ms=required_deadline_ms)
    if agg_trades is None:
        return _incomplete("agg_trades_missing", required_deadline_ms=required_deadline_ms)
    try:
        trades = _normalize_rows(agg_trades)
    except (TypeError, ValueError, OverflowError) as exc:
        return _incomplete(f"invalid_agg_trade:{exc}", required_deadline_ms=required_deadline_ms)

    window_trades = tuple(
        trade
        for trade in trades
        if parsed_sample.start_ms <= trade.time_ms <= parsed_sample.outcome_deadline_ms
    )
    fill = _find_fill(window_trades, parsed_sample)
    touch = _touch_at_or_after_fill(window_trades, parsed_sample, fill) if fill else None
    if fill is not None:
        required_deadline_ms = (
            touch.trade.time_ms
            if touch is not None and touch.trade is not None
            else parsed_sample.outcome_deadline_ms
        )
    if coverage_start > parsed_sample.start_ms:
        return _incomplete("coverage_starts_after_sample", required_deadline_ms=required_deadline_ms)
    if coverage_end < required_deadline_ms:
        return _incomplete("coverage_ends_before_required_deadline", required_deadline_ms=required_deadline_ms)
    gap_reason = _coverage_gap_reason(
        coverage_gaps_ms,
        parsed_sample.start_ms,
        required_deadline_ms,
    )
    if gap_reason is None:
        gap_reason = _first_gap_reason(window_trades, parsed_sample.start_ms, required_deadline_ms)
    if gap_reason is not None:
        return _incomplete(gap_reason, required_deadline_ms=required_deadline_ms)

    model = cost_model or ShadowCostModel()
    if not isinstance(model, ShadowCostModel):
        return _incomplete("invalid_cost_model", required_deadline_ms=required_deadline_ms)
    base: dict[str, Any] = {
        "evidence_source": "binance_aggTrade",
        "status": "resolved",
        "evaluable": True,
        "data_complete": True,
        "fill_model": parsed_sample.fill_model,
        "data_quality": {
            "status": "COMPLETE",
            "complete": True,
            "reason": None,
            "coverage_start_ms": coverage_start,
            "coverage_end_ms": coverage_end,
            "required_deadline_ms": required_deadline_ms,
            "unique_trade_count": len(window_trades),
        },
        "cost_model": {
            "entry_liquidity": "MAKER",
            "tp_liquidity": "MAKER",
            "sl_liquidity": "TAKER",
            "max_hold_liquidity": "TAKER",
            "maker_fee_rate": float(model.maker_fee_rate),
            "taker_fee_rate": float(model.taker_fee_rate),
            "slippage_bp": float(model.slippage_bp),
        },
    }
    if fill is None:
        return {
            **base,
            "shadow_outcome": ShadowEvidenceOutcome.NO_FILL.value,
            "first_touch_result": ShadowEvidenceOutcome.NO_FILL.name,
            "filled": False,
            "filled_ts": None,
            "fill_trade_id": None,
            "resolved_ts": parsed_sample.entry_deadline_ms,
            "hit_time_ms": parsed_sample.entry_deadline_ms,
            "elapsed_s": (parsed_sample.entry_deadline_ms - parsed_sample.start_ms) / 1_000.0,
            "exit_reference_price": None,
            "exit_liquidity": None,
            "mfe_bp": 0.0,
            "mae_bp": 0.0,
            "paper_pnl_bp_before_fee": 0.0,
            "paper_pnl_bp_after_fee": 0.0,
            "paper_pnl_usdc_before_cost": 0.0,
            "paper_pnl_usdc_after_fee": 0.0,
            "entry_fee_usdc": 0.0,
            "exit_fee_usdc": 0.0,
            "estimated_fee_usdc": 0.0,
            "estimated_fee_bp": 0.0,
            "estimated_slippage_usdc": 0.0,
            "conservative_slippage_buffer_bp": 0.0,
            "fee_model": "no_fill_zero_cost",
            "ambiguity_flag": False,
        }

    assert touch is not None
    if touch.outcome is ShadowEvidenceOutcome.TP_FIRST:
        exit_price = parsed_sample.tp_price
        exit_liquidity = "MAKER"
        terminal_ms = touch.trade.time_ms if touch.trade else parsed_sample.outcome_deadline_ms
    elif touch.outcome is ShadowEvidenceOutcome.SL_FIRST:
        exit_price = parsed_sample.sl_price
        exit_liquidity = "TAKER"
        terminal_ms = touch.trade.time_ms if touch.trade else parsed_sample.outcome_deadline_ms
    else:
        terminal_ms = parsed_sample.outcome_deadline_ms
        observed = [
            trade
            for trade in window_trades
            if (trade.time_ms, trade.agg_trade_id) >= (fill.time_ms, fill.agg_trade_id)
            and trade.time_ms <= terminal_ms
        ]
        exit_price = observed[-1].price if observed else parsed_sample.entry_price
        exit_liquidity = "TAKER"
    try:
        mfe_bp, mae_bp = _path_metrics(window_trades, parsed_sample, fill, terminal_ms)
        cost_payload = _costs(
            parsed_sample,
            exit_price=exit_price,
            exit_liquidity=exit_liquidity,
            cost_model=model,
        )
    except (ArithmeticError, ValueError, OverflowError) as exc:
        return _incomplete(
            f"invalid_calculation:{exc}",
            required_deadline_ms=required_deadline_ms,
        )
    return {
        **base,
        "shadow_outcome": touch.outcome.value,
        "first_touch_result": touch.outcome.name,
        "filled": True,
        "filled_ts": fill.time_ms,
        "fill_trade_id": fill.agg_trade_id,
        "fill_trade_price": fill.price,
        "resolved_ts": terminal_ms,
        "hit_time_ms": touch.trade.time_ms if touch.trade else terminal_ms,
        "touch_trade_id": touch.trade.agg_trade_id if touch.trade else None,
        "touch_trade_price": touch.trade.price if touch.trade else None,
        "elapsed_s": (terminal_ms - parsed_sample.start_ms) / 1_000.0,
        "entry_fill_age_s": (fill.time_ms - parsed_sample.start_ms) / 1_000.0,
        "exit_reference_price": exit_price,
        "exit_liquidity": exit_liquidity,
        "tp_hit": touch.outcome is ShadowEvidenceOutcome.TP_FIRST,
        "sl_hit": touch.outcome is ShadowEvidenceOutcome.SL_FIRST,
        "ambiguity_flag": touch.ambiguous_same_timestamp,
        "ambiguity_resolution": "SL_FIRST" if touch.ambiguous_same_timestamp else None,
        "mfe_bp": mfe_bp,
        "mae_bp": mae_bp,
        **cost_payload,
    }


# Short alias for callers that do not encode the version in local names.
evaluate_shadow_evidence = evaluate_v1461_shadow_evidence


__all__ = [
    "ShadowCostModel",
    "ShadowEvidenceOutcome",
    "evaluate_shadow_evidence",
    "evaluate_v1461_shadow_evidence",
]
