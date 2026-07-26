"""Pure helpers for v1.4.58 STUP maker-fill shadow telemetry."""

from __future__ import annotations

from math import isfinite
from typing import Any, Mapping, Sequence


def build_stup_fill_shadow_samples(
    *,
    group_id: str,
    run_id: str,
    session_id: str,
    symbol: str,
    side: str,
    signal_price: float,
    tick_size: float,
    start_ms: int,
    decision_latency_ms: int,
    ttl_seconds: int,
    notional_usdc: float,
    tp_pct: float,
    sl_pct: float,
    partial_exit_pct: float,
    action_id: str,
    variants: Mapping[str, float],
) -> list[dict[str, Any]]:
    normalized_side = str(side or "").upper()
    if normalized_side not in {"LONG", "SHORT"}:
        return []
    if not _positive(signal_price) or not _positive(tick_size) or not group_id or not run_id:
        return []
    latency_ms = max(0, int(decision_latency_ms))
    ttl_ms = max(1, int(ttl_seconds)) * 1000
    eligible_after_ms = int(start_ms) + latency_ms
    expires_ms = int(start_ms) + ttl_ms
    samples: list[dict[str, Any]] = []
    for variant, offset_raw in variants.items():
        try:
            offset_bp = float(offset_raw)
        except (TypeError, ValueError):
            continue
        if not isfinite(offset_bp) or offset_bp < 0:
            continue
        direction = 1.0 if normalized_side == "SHORT" else -1.0
        entry_price = float(signal_price) * (1.0 + direction * offset_bp / 10_000.0)
        trade_through_price = (
            entry_price + float(tick_size)
            if normalized_side == "SHORT"
            else entry_price - float(tick_size)
        )
        samples.append(
            {
                "sample_id": f"{group_id}:{variant}",
                "group_id": group_id,
                "run_id": run_id,
                "adaptive_session_id": session_id,
                "symbol": symbol,
                "side": normalized_side,
                "variant": variant,
                "action_id": action_id,
                "entry_offset_bp": offset_bp,
                "entry_price": round(entry_price, 8),
                "trade_through_price": round(trade_through_price, 8),
                "tick_size": float(tick_size),
                "start_ms": int(start_ms),
                "eligible_after_ms": eligible_after_ms,
                "expires_ms": expires_ms,
                "entry_ttl_s": max(1, int(ttl_seconds)),
                "decision_latency_ms": latency_ms,
                "fill_model": "aggtrade_one_tick_through_after_decision_latency",
                "requested_notional_usdc": float(notional_usdc),
                "tp_pct": float(tp_pct),
                "sl_pct": float(sl_pct),
                "partial_exit_pct": float(partial_exit_pct),
                "cursor_ms": eligible_after_ms,
                "next_from_id": None,
            }
        )
    return samples


def first_stup_shadow_fill(
    sample: Mapping[str, Any],
    trades: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    side = str(sample.get("side") or "").upper()
    if side not in {"LONG", "SHORT"}:
        return None
    try:
        eligible_after_ms = int(sample["eligible_after_ms"])
        expires_ms = int(sample["expires_ms"])
        threshold = float(sample["trade_through_price"])
        start_ms = int(sample["start_ms"])
    except (KeyError, TypeError, ValueError):
        return None
    parsed: list[tuple[int, int, float]] = []
    for trade in trades:
        try:
            trade_ms = int(trade.get("T", trade.get("time")))
            trade_id = int(trade.get("a", trade.get("id", -1)))
            price = float(trade.get("p", trade.get("price")))
        except (TypeError, ValueError):
            continue
        if eligible_after_ms <= trade_ms <= expires_ms and isfinite(price):
            parsed.append((trade_ms, trade_id, price))
    for trade_ms, trade_id, price in sorted(parsed):
        crossed = price >= threshold if side == "SHORT" else price <= threshold
        if crossed:
            return {
                "outcome": "filled",
                "filled": True,
                "fill_trade_id": trade_id,
                "fill_trade_price": price,
                "filled_at_ms": trade_ms,
                "fill_age_ms": max(0, trade_ms - start_ms),
                "no_fill_pnl_usdc": None,
            }
    return None


def no_fill_outcome(sample: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "outcome": "no_fill",
        "filled": False,
        "fill_trade_id": None,
        "fill_trade_price": None,
        "filled_at_ms": None,
        "fill_age_ms": None,
        "resolved_at_ms": int(sample.get("expires_ms") or 0),
        "no_fill_pnl_usdc": 0.0,
    }


def _positive(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(float(value))
        and float(value) > 0.0
    )
