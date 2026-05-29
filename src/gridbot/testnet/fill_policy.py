"""Shared entry fill-policy helpers for testnet execution analysis."""

from __future__ import annotations

VALID_ENTRY_FILL_POLICIES = {"strict", "limit_tolerance"}


def normalize_entry_fill_policy(policy: str | None) -> str:
    value = (policy or "limit_tolerance").strip().lower()
    if value in {"none", "original", "trend350", "trend350_strict"}:
        return "strict"
    if value in {"tolerance", "maker_tolerance", "limit_tolerance"}:
        return "limit_tolerance"
    return "strict"


def effective_entry_tolerance_bps(
    policy: str | None,
    configured_bps: float,
    *,
    score: int | None = None,
    min_score: int = 0,
) -> float:
    """Return the tolerance that may be applied for a signal."""
    if normalize_entry_fill_policy(policy) != "limit_tolerance":
        return 0.0
    if score is not None and min_score > 0 and score < min_score:
        return 0.0
    return max(0.0, float(configured_bps))


def entry_limit_price(
    direction: str,
    planned_entry: float,
    planned_stop: float,
    planned_take_profit: float,
    tolerance_bps: float,
) -> float:
    """Move a passive limit slightly toward market without crossing stop/TP."""
    tolerance = max(0.0, float(tolerance_bps))
    if tolerance <= 0 or planned_entry <= 0:
        return planned_entry

    shift = planned_entry * tolerance / 10_000
    epsilon = max(planned_entry * 0.000001, 0.0001)
    if direction == "short":
        candidate = planned_entry - shift
        if planned_take_profit > 0:
            candidate = max(candidate, planned_take_profit + epsilon)
        if planned_stop > 0:
            candidate = min(candidate, planned_stop - epsilon)
    else:
        candidate = planned_entry + shift
        if planned_take_profit > 0:
            candidate = min(candidate, planned_take_profit - epsilon)
        if planned_stop > 0:
            candidate = max(candidate, planned_stop + epsilon)
    return round(max(candidate, 0.0), 8)


def reward_pct_for_entry(entry: float, take_profit: float, direction: str) -> float:
    if entry <= 0 or take_profit <= 0:
        return 0.0
    reward_distance = entry - take_profit if direction == "short" else take_profit - entry
    return max(reward_distance, 0.0) / entry * 100

