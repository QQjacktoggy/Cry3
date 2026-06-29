
"""Shadow-only TP allocation simulator for Codex V1.3.2.

The module is intentionally pure: it does not place orders and does not mutate
mainnet state.  It replays baseline and TP allocation variants on the same
entry sample/path, then emits paired delta payloads for DB logging.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


CODEX_TP_POLICY_VERSION = "_codex_v1.3.2_tp_policy_shadow_optimizer"
TP_POLICY_TP1_BP = 5.0
TP_POLICY_PATH_TTL_S = 900
DEFAULT_SHADOW_ENTRY_TTL_S = 180
TP_POLICY_PATH_QUALITY = "candle_1m"
BASELINE_FIELDS = (
    "baseline_tp1_bp",
    "baseline_tp1_qty_frac",
    "baseline_mid_tp_bp",
    "baseline_mid_qty_frac",
    "baseline_full_tp_bp",
    "baseline_full_tp_qty_frac",
    "baseline_runner_qty_frac",
    "baseline_trail_arm_frac",
    "baseline_trail_giveback_frac",
    "baseline_sl_bp",
    "baseline_fee_model",
    "baseline_order_plan_hash",
)


def _price(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def stable_hash(*parts: object, length: int = 16) -> str:
    payload = "|".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def gross_pnl_bp(side: str, entry: float, exit_price: float) -> float:
    if entry <= 0 or exit_price <= 0:
        return 0.0
    if side == "LONG":
        return (exit_price / entry - 1.0) * 10_000.0
    return (entry / exit_price - 1.0) * 10_000.0


def path_bp(side: str, entry: float, observed_high: float, observed_low: float) -> tuple[float, float]:
    if entry <= 0:
        return 0.0, 0.0
    if side == "LONG":
        return (
            max(0.0, (observed_high - entry) / entry * 10_000.0),
            max(0.0, (entry - observed_low) / entry * 10_000.0),
        )
    return (
        max(0.0, (entry - observed_low) / entry * 10_000.0),
        max(0.0, (observed_high - entry) / entry * 10_000.0),
    )


def side_price(entry: float, side: str, bp: float | None) -> float | None:
    if bp is None or entry <= 0:
        return None
    return entry * (1 + bp / 10_000.0) if side == "LONG" else entry * (1 - bp / 10_000.0)


def price_bp(side: str, entry: float, target: float | None) -> float | None:
    if entry <= 0 or target is None or target <= 0:
        return None
    if side == "LONG":
        return (target / entry - 1.0) * 10_000.0
    return (entry / target - 1.0) * 10_000.0


def adverse_bp(side: str, entry: float, stop: float | None) -> float | None:
    if entry <= 0 or stop is None or stop <= 0:
        return None
    if side == "LONG":
        return (entry / stop - 1.0) * 10_000.0
    return (stop / entry - 1.0) * 10_000.0


def _feature_float(features: Mapping[str, Any], key: str) -> float | None:
    try:
        return float(features.get(key))
    except (TypeError, ValueError):
        return None


def _hash_baseline(baseline: Mapping[str, Any]) -> str:
    payload = {
        key: baseline.get(key)
        for key in BASELINE_FIELDS
        if key != "baseline_order_plan_hash"
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "tpplan_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _baseline_from_mapping(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    if not payload.get("baseline_order_plan_hash"):
        return None
    required = (
        "baseline_tp1_qty_frac",
        "baseline_mid_qty_frac",
        "baseline_full_tp_qty_frac",
        "baseline_runner_qty_frac",
        "baseline_full_tp_bp",
        "baseline_sl_bp",
    )
    if any(payload.get(key) is None for key in required):
        return None
    baseline = {key: payload.get(key) for key in BASELINE_FIELDS if key in payload}
    baseline.setdefault("baseline_tp1_bp", TP_POLICY_TP1_BP)
    baseline.setdefault("baseline_mid_tp_bp", None)
    baseline.setdefault("baseline_trail_arm_frac", 0.7)
    baseline.setdefault("baseline_trail_giveback_frac", 0.25)
    baseline.setdefault("baseline_fee_model", "maker_taker_estimate")
    return baseline


def baseline_snapshot(settings: Any, sample: Mapping[str, Any]) -> dict[str, Any] | None:
    entry = _price(sample.get("entry_price"))
    full_tp = _price(sample.get("tp_price"))
    stop = _price(sample.get("sl_price"))
    side = str(sample.get("side") or "").upper()
    if entry is None or full_tp is None or stop is None or side not in {"LONG", "SHORT"}:
        return None

    partial_qty = max(0.0, min(1.0, float(getattr(settings, "mainnet_partial_exit_pct", 0.0) or 0.0)))
    remaining = max(0.0, 1.0 - partial_qty)
    mid_pct = float(getattr(settings, "mainnet_mid_tp_pct", 0.0) or 0.0)
    mid_exit_pct = max(0.0, min(1.0, float(getattr(settings, "mainnet_mid_exit_pct", 0.0) or 0.0)))
    if mid_pct > 0 and mid_exit_pct > 0:
        mid_qty = remaining * mid_exit_pct
        remaining = max(0.0, remaining - mid_qty)
        full_qty = remaining
        runner_qty = 0.0
        mid_bp = mid_pct * 10_000.0
    else:
        mid_qty = 0.0
        full_qty = remaining * mid_exit_pct
        runner_qty = max(0.0, remaining - full_qty)
        mid_bp = None

    baseline = {
        "baseline_tp1_bp": TP_POLICY_TP1_BP,
        "baseline_tp1_qty_frac": round(partial_qty, 8),
        "baseline_mid_tp_bp": round(mid_bp, 8) if mid_bp is not None else None,
        "baseline_mid_qty_frac": round(mid_qty, 8),
        "baseline_full_tp_bp": round(float(price_bp(side, entry, full_tp) or 0.0), 8),
        "baseline_full_tp_qty_frac": round(full_qty, 8),
        "baseline_runner_qty_frac": round(runner_qty, 8),
        "baseline_trail_arm_frac": float(getattr(settings, "mainnet_trail_arm_frac", 0.7) or 0.7),
        "baseline_trail_giveback_frac": float(getattr(settings, "mainnet_trail_giveback_frac", 0.25) or 0.25),
        "baseline_sl_bp": round(float(adverse_bp(side, entry, stop) or 0.0), 8),
        "baseline_fee_model": "maker_taker_estimate",
    }
    baseline["baseline_order_plan_hash"] = _hash_baseline(baseline)
    return baseline


def baseline_snapshot_from_order_plan(
    settings: Any,
    sample: Mapping[str, Any],
    *,
    current_qty: float,
    orders: Sequence[Mapping[str, Any] | Sequence[Any]],
) -> dict[str, Any] | None:
    entry = _price(sample.get("entry_price"))
    fallback_full_tp = _price(sample.get("tp_price"))
    stop = _price(sample.get("sl_price"))
    side = str(sample.get("side") or "").upper()
    if entry is None or fallback_full_tp is None or stop is None or side not in {"LONG", "SHORT"} or current_qty <= 0:
        return None

    qty_by_layer = {"tp1": 0.0, "tp2": 0.0, "tp3": 0.0}
    price_by_layer: dict[str, float] = {}
    for order in orders:
        if isinstance(order, Mapping):
            client_id = str(order.get("client_order_id") or order.get("clientOrderId") or "")
            qty_raw = order.get("qty") or order.get("origQty") or order.get("quantity")
            price_raw = order.get("price")
        else:
            values = list(order)
            if len(values) < 3:
                continue
            client_id = str(values[0])
            qty_raw = values[1]
            price_raw = values[2]
        try:
            qty = max(0.0, float(qty_raw))
            price = float(price_raw)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or price <= 0:
            continue
        if client_id.endswith("_tp1"):
            layer = "tp1"
        elif client_id.endswith("_tp2"):
            layer = "tp2"
        elif client_id.endswith("_tp3"):
            layer = "tp3"
        else:
            continue
        qty_by_layer[layer] += qty
        price_by_layer[layer] = price

    if not any(qty_by_layer.values()):
        return None
    tp1_qty = min(1.0, qty_by_layer["tp1"] / current_qty)
    mid_qty = min(max(0.0, 1.0 - tp1_qty), qty_by_layer["tp2"] / current_qty)
    full_qty = min(max(0.0, 1.0 - tp1_qty - mid_qty), qty_by_layer["tp3"] / current_qty)
    runner_qty = max(0.0, 1.0 - tp1_qty - mid_qty - full_qty)
    tp1_bp = price_bp(side, entry, price_by_layer.get("tp1")) if qty_by_layer["tp1"] > 0 else TP_POLICY_TP1_BP
    mid_bp = price_bp(side, entry, price_by_layer.get("tp2")) if qty_by_layer["tp2"] > 0 else None
    full_bp = price_bp(side, entry, price_by_layer.get("tp3") or fallback_full_tp)
    baseline = {
        "baseline_tp1_bp": round(float(tp1_bp if tp1_bp is not None else TP_POLICY_TP1_BP), 8),
        "baseline_tp1_qty_frac": round(tp1_qty, 8),
        "baseline_mid_tp_bp": round(float(mid_bp), 8) if mid_bp is not None else None,
        "baseline_mid_qty_frac": round(mid_qty, 8),
        "baseline_full_tp_bp": round(float(full_bp or 0.0), 8),
        "baseline_full_tp_qty_frac": round(full_qty, 8),
        "baseline_runner_qty_frac": round(runner_qty, 8),
        "baseline_trail_arm_frac": float(getattr(settings, "mainnet_trail_arm_frac", 0.7) or 0.7),
        "baseline_trail_giveback_frac": float(getattr(settings, "mainnet_trail_giveback_frac", 0.25) or 0.25),
        "baseline_sl_bp": round(float(adverse_bp(side, entry, stop) or 0.0), 8),
        "baseline_fee_model": "maker_taker_estimate",
    }
    baseline["baseline_order_plan_hash"] = _hash_baseline(baseline)
    return baseline


def policy_definitions(settings: Any, baseline: Mapping[str, Any]) -> list[dict[str, Any]]:
    full_bp = baseline.get("baseline_full_tp_bp")
    trail_arm = float(getattr(settings, "mainnet_trail_arm_frac", 0.7) or 0.7)
    trail_giveback = float(getattr(settings, "mainnet_trail_giveback_frac", 0.25) or 0.25)
    return [
        {
            "tp_policy_id": "baseline",
            "tp1_bp": baseline.get("baseline_tp1_bp", TP_POLICY_TP1_BP),
            "tp1_qty_frac": baseline.get("baseline_tp1_qty_frac", 0.40),
            "mid_tp_bp": baseline.get("baseline_mid_tp_bp"),
            "mid_qty_frac": baseline.get("baseline_mid_qty_frac", 0.0),
            "full_tp_bp": full_bp,
            "full_tp_qty_frac": baseline.get("baseline_full_tp_qty_frac", 0.30),
            "runner_qty_frac": baseline.get("baseline_runner_qty_frac", 0.30),
            "trail_arm_frac": baseline.get("baseline_trail_arm_frac", trail_arm),
            "trail_giveback_frac": baseline.get("baseline_trail_giveback_frac", trail_giveback),
        },
        {
            "tp_policy_id": "profit_a_runner40",
            "tp1_bp": TP_POLICY_TP1_BP,
            "tp1_qty_frac": 0.35,
            "mid_tp_bp": None,
            "mid_qty_frac": 0.0,
            "full_tp_bp": full_bp,
            "full_tp_qty_frac": 0.25,
            "runner_qty_frac": 0.40,
            "trail_arm_frac": trail_arm,
            "trail_giveback_frac": trail_giveback,
        },
        {
            "tp_policy_id": "profit_b_runner45",
            "tp1_bp": TP_POLICY_TP1_BP,
            "tp1_qty_frac": 0.30,
            "mid_tp_bp": None,
            "mid_qty_frac": 0.0,
            "full_tp_bp": full_bp,
            "full_tp_qty_frac": 0.25,
            "runner_qty_frac": 0.45,
            "trail_arm_frac": trail_arm,
            "trail_giveback_frac": trail_giveback,
        },
        {
            "tp_policy_id": "w6a_safe",
            "tp1_bp": TP_POLICY_TP1_BP,
            "tp1_qty_frac": 0.45,
            "mid_tp_bp": None,
            "mid_qty_frac": 0.0,
            "full_tp_bp": full_bp,
            "full_tp_qty_frac": 0.30,
            "runner_qty_frac": 0.25,
            "trail_arm_frac": trail_arm,
            "trail_giveback_frac": trail_giveback,
        },
        {
            "tp_policy_id": "mid_restore_8bp",
            "tp1_bp": TP_POLICY_TP1_BP,
            "tp1_qty_frac": 0.40,
            "mid_tp_bp": 8.0,
            "mid_qty_frac": 0.20,
            "full_tp_bp": full_bp,
            "full_tp_qty_frac": 0.20,
            "runner_qty_frac": 0.20,
            "trail_arm_frac": trail_arm,
            "trail_giveback_frac": trail_giveback,
        },
    ]


def validate_policy(policy: Mapping[str, Any]) -> None:
    qty_sum = sum(
        float(policy.get(field) or 0.0)
        for field in ("tp1_qty_frac", "mid_qty_frac", "full_tp_qty_frac", "runner_qty_frac")
    )
    if abs(qty_sum - 1.0) > 1e-6:
        raise ValueError(f"TP policy {policy.get('tp_policy_id')} qty fractions sum to {qty_sum:.6f}")
    if policy.get("mid_qty_frac") and policy.get("mid_tp_bp") is None:
        raise ValueError(f"TP policy {policy.get('tp_policy_id')} has mid qty without mid TP")


def cohort_id(sample: Mapping[str, Any], baseline: Mapping[str, Any]) -> str:
    lane_family = str(sample.get("shadow_lane_family") or sample.get("lane_code") or sample.get("candidate_lane") or "UNKNOWN")
    side = str(sample.get("side") or "UNKNOWN").upper()
    strategy = str(sample.get("strategy") or "UNKNOWN")
    fill_model = str(sample.get("fill_model") or "limit_touch")
    full_bp = float(baseline.get("baseline_full_tp_bp") or 0.0)
    stop_bp = float(baseline.get("baseline_sl_bp") or 0.0)
    return f"{lane_family}|{side}|{strategy}|{fill_model}|fulltp_{round(full_bp)}bp|sl_{round(stop_bp)}bp|v1.3"


def build_active_sample(
    settings: Any,
    sample: Mapping[str, Any],
    *,
    source_type: str,
    actual_live_pnl_bp_after_fee: float | None = None,
    baseline_override: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    baseline = dict(baseline_override) if baseline_override else baseline_snapshot(settings, sample)
    if not baseline:
        return None
    policies = policy_definitions(settings, baseline)
    for policy in policies:
        validate_policy(policy)
    paired_sample_id = str(sample.get("sample_id") or sample.get("run_id") or "")
    if not paired_sample_id:
        return None
    path_ttl_s = int(getattr(settings, "mainnet_codex_tp_policy_path_ttl_s", TP_POLICY_PATH_TTL_S) or TP_POLICY_PATH_TTL_S)
    path_id = "tppath_" + stable_hash(
        paired_sample_id,
        sample.get("start_ms"),
        path_ttl_s,
        TP_POLICY_PATH_QUALITY,
        baseline.get("baseline_order_plan_hash"),
    )
    return {
        **dict(sample),
        **baseline,
        "event_type": "tp_policy_shadow_started",
        "version": CODEX_TP_POLICY_VERSION,
        "source_type": source_type,
        "paired_sample_id": paired_sample_id,
        "tp_policy_path_id": path_id,
        "tp_policy_cohort_id": cohort_id(sample, baseline),
        "policy_observation_ttl_s": path_ttl_s,
        "path_quality": TP_POLICY_PATH_QUALITY,
        "actual_live_pnl_bp_after_fee": actual_live_pnl_bp_after_fee,
        "live_tp_override_enabled": False,
        "tp_policy_definitions": policies,
    }


def policy_prices(active: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, float | None]:
    entry = float(active.get("entry_price") or 0.0)
    side = str(active.get("side") or "").upper()
    full_tp = _price(active.get("tp_price"))
    stop = _price(active.get("sl_price"))
    tp1 = side_price(entry, side, float(policy.get("tp1_bp") or TP_POLICY_TP1_BP))
    mid = side_price(entry, side, policy.get("mid_tp_bp"))
    if full_tp and tp1:
        tp1 = min(tp1, full_tp) if side == "LONG" else max(tp1, full_tp)
    if full_tp and mid:
        mid = min(mid, full_tp) if side == "LONG" else max(mid, full_tp)
    return {"tp1": tp1, "mid": mid, "full": full_tp, "sl": stop}


def _exit_component(
    exits: list[dict[str, Any]],
    component: str,
    qty_frac: float,
    exit_price: float,
    exit_type: str,
    side: str,
    entry: float,
) -> None:
    if qty_frac <= 0:
        return
    exits.append(
        {
            "component": component,
            "qty_frac": round(qty_frac, 8),
            "exit_type": exit_type,
            "exit_price": round(exit_price, 8),
            "pnl_bp": round(gross_pnl_bp(side, entry, exit_price), 8),
        }
    )


def _no_fill_result(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tp_policy_id": policy.get("tp_policy_id"),
        "filled": False,
        "filled_ts": None,
        "tp1_touched": False,
        "mid_tp_touched": False,
        "full_tp_touched": False,
        "trail_armed": False,
        "runner_exit_type": None,
        "path_end_reason": "no_fill",
        "ambiguous_path": False,
        "ambiguous_stage": None,
        "primary_promotion_eligible": False,
        "stress_mode_result": None,
        "component_exits": [],
        "policy_pnl_bp_before_fee": 0.0,
        "policy_pnl_bp_after_fee": 0.0,
        "entry_fee_bp": 0.0,
        "tp1_exit_fee_bp": 0.0,
        "mid_exit_fee_bp": 0.0,
        "full_exit_fee_bp": 0.0,
        "runner_exit_fee_bp": 0.0,
        "total_fee_bp": 0.0,
        "mfe_bp": 0.0,
        "mae_bp": 0.0,
        "runner_peak_bp": 0.0,
        "runner_exit_bp": 0.0,
        "giveback_bp": 0.0,
        "mfe_capture_ratio": 0.0,
    }


def simulate_policy(
    settings: Any,
    active: Mapping[str, Any],
    candles: Sequence[Any],
    policy: Mapping[str, Any],
    *,
    force_terminal: bool = False,
    terminal_reason: str | None = None,
) -> dict[str, Any] | None:
    entry = _price(active.get("entry_price"))
    side = str(active.get("side") or "").upper()
    prices = policy_prices(active, policy)
    tp1 = prices.get("tp1")
    mid_tp = prices.get("mid")
    full_tp = prices.get("full")
    stop = prices.get("sl")
    if entry is None or tp1 is None or full_tp is None or stop is None or side not in {"LONG", "SHORT"}:
        return None

    start_ms = int(active.get("start_ms") or 0)
    entry_ttl_s = int(active.get("entry_ttl_s") or DEFAULT_SHADOW_ENTRY_TTL_S)
    path_ttl_s = int(active.get("policy_observation_ttl_s") or TP_POLICY_PATH_TTL_S)
    entry_expiry_ms = start_ms + max(0, entry_ttl_s) * 1000
    path_expiry_ms = start_ms + max(1, path_ttl_s) * 1000
    fill_model = str(active.get("fill_model") or "limit_touch")
    filled = fill_model == "immediate_shadow"
    filled_ms = start_ms if filled else None
    post_tp1 = False
    tp1_touched_flag = False

    open_components = {
        "tp1": float(policy.get("tp1_qty_frac") or 0.0),
        "mid": float(policy.get("mid_qty_frac") or 0.0),
        "full": float(policy.get("full_tp_qty_frac") or 0.0),
        "runner": float(policy.get("runner_qty_frac") or 0.0),
    }
    exits: list[dict[str, Any]] = []
    observed_high: float | None = None
    observed_low: float | None = None
    last_close: float | None = None
    last_close_ms: int | None = None
    ambiguous_path = False
    ambiguous_stage: str | None = None
    trail_armed = False
    runner_peak = entry
    runner_exit_type: str | None = None
    path_end_reason = "pending"

    for candle in sorted(candles, key=lambda item: int(item.open_time_ms)):
        open_ms = int(candle.open_time_ms)
        close_ms = open_ms + 60_000
        if close_ms <= start_ms:
            continue
        if open_ms < start_ms:
            continue
        if open_ms >= path_expiry_ms:
            break

        high = float(candle.high)
        low = float(candle.low)
        last_close = float(candle.close)
        last_close_ms = close_ms

        if not filled:
            if close_ms > entry_expiry_ms:
                return _no_fill_result(policy)
            filled = low <= entry if side == "LONG" else high >= entry
            if filled:
                filled_ms = close_ms
                continue
            continue

        if close_ms > path_expiry_ms:
            break
        observed_high = high if observed_high is None else max(observed_high, high)
        observed_low = low if observed_low is None else min(observed_low, low)

        if not post_tp1:
            if side == "LONG":
                tp1_hit = high >= tp1
                sl_hit = low <= stop
            else:
                tp1_hit = low <= tp1
                sl_hit = high >= stop
            if tp1_hit and sl_hit:
                tp1_touched_flag = True
                ambiguous_path = True
                ambiguous_stage = "pre_tp1"
                for component, qty_frac in list(open_components.items()):
                    _exit_component(exits, component, qty_frac, stop, "sl", side, entry)
                    open_components[component] = 0.0
                path_end_reason = "ambiguous_terminal"
                break
            if sl_hit:
                for component, qty_frac in list(open_components.items()):
                    _exit_component(exits, component, qty_frac, stop, "sl", side, entry)
                    open_components[component] = 0.0
                path_end_reason = "sl_before_tp1"
                break
            if tp1_hit:
                tp1_touched_flag = True
                _exit_component(exits, "tp1", open_components.get("tp1", 0.0), tp1, "tp1", side, entry)
                open_components["tp1"] = 0.0
                post_tp1 = True
                runner_peak = high if side == "LONG" else low
                continue
            continue
        mid_qty = open_components.get("mid", 0.0)
        full_qty = open_components.get("full", 0.0)
        runner_qty = open_components.get("runner", 0.0)
        if side == "LONG":
            sl_hit = low <= stop
            mid_hit = bool(mid_tp and mid_qty > 0 and high >= mid_tp)
            full_hit = bool(full_qty > 0 and high >= full_tp)
            favorable_peak = high
        else:
            sl_hit = high >= stop
            mid_hit = bool(mid_tp and mid_qty > 0 and low <= mid_tp)
            full_hit = bool(full_qty > 0 and low <= full_tp)
            favorable_peak = low

        runner_trail_hit = False
        runner_trail_same_bar_arm = False
        trail_stop: float | None = None
        next_trail_armed = trail_armed
        next_runner_peak = runner_peak
        if runner_qty > 0:
            if side == "LONG":
                next_runner_peak = max(runner_peak, favorable_peak)
                peak_bp = (next_runner_peak - entry) / entry * 10_000.0
            else:
                next_runner_peak = min(runner_peak, favorable_peak)
                peak_bp = (entry - next_runner_peak) / entry * 10_000.0
            arm_bp = max(0.0, float(policy.get("full_tp_bp") or 0.0)) * float(policy.get("trail_arm_frac") or 0.0)
            if not next_trail_armed and peak_bp >= arm_bp > 0:
                next_trail_armed = True
                runner_trail_same_bar_arm = not trail_armed
            if next_trail_armed:
                keep = 1.0 - float(policy.get("trail_giveback_frac") or 0.0)
                floor_bp = float(getattr(settings, "mainnet_trail_profit_floor_bp", 0.0) or 0.0)
                if side == "LONG":
                    trail_stop = entry + (next_runner_peak - entry) * keep
                    runner_trail_hit = low <= trail_stop and trail_stop > entry * (1 + floor_bp / 10_000.0)
                else:
                    trail_stop = entry - (entry - next_runner_peak) * keep
                    runner_trail_hit = high >= trail_stop and trail_stop < entry * (1 - floor_bp / 10_000.0)

        if sl_hit and (mid_hit or full_hit or runner_trail_hit):
            ambiguous_path = True
            ambiguous_stage = "trail_vs_sl" if runner_trail_hit else "post_tp1"
            for component, qty_frac in list(open_components.items()):
                if qty_frac > 0:
                    _exit_component(exits, component, qty_frac, stop, "sl", side, entry)
                    open_components[component] = 0.0
            path_end_reason = "ambiguous_terminal"
            break
        if runner_trail_same_bar_arm and runner_trail_hit:
            ambiguous_path = True
            ambiguous_stage = "trail_same_bar_arm"
        if sl_hit:
            for component, qty_frac in list(open_components.items()):
                if qty_frac > 0:
                    _exit_component(exits, component, qty_frac, stop, "sl", side, entry)
                    open_components[component] = 0.0
            path_end_reason = "sl_after_tp1"
            break
        if mid_hit and mid_tp is not None:
            _exit_component(exits, "mid", mid_qty, mid_tp, "mid_tp", side, entry)
            open_components["mid"] = 0.0
        if full_hit:
            _exit_component(exits, "full", full_qty, full_tp, "full_tp", side, entry)
            open_components["full"] = 0.0

        runner_qty = open_components.get("runner", 0.0)
        if runner_qty > 0:
            runner_peak = next_runner_peak
            trail_armed = next_trail_armed
            if runner_trail_hit and trail_stop is not None:
                _exit_component(exits, "runner", runner_qty, trail_stop, "trail", side, entry)
                open_components["runner"] = 0.0
                runner_exit_type = "trail"
        if sum(open_components.values()) <= 1e-9:
            path_end_reason = "all_policies_terminal"
            break

    if filled and sum(open_components.values()) > 1e-9:
        if last_close_ms is not None and (last_close_ms >= path_expiry_ms or force_terminal):
            ttl_price = last_close if last_close is not None else entry
            for component, qty_frac in list(open_components.items()):
                if qty_frac > 0:
                    _exit_component(exits, component, qty_frac, ttl_price, "ttl_mark", side, entry)
                    open_components[component] = 0.0
            path_end_reason = terminal_reason or "ttl_expired"
        else:
            return None
    if not filled:
        return None
    if observed_high is None or observed_low is None:
        observed_high = entry
        observed_low = entry

    mfe_bp, mae_bp = path_bp(side, entry, observed_high, observed_low)
    weighted_pnl = sum(float(row["qty_frac"]) * float(row["pnl_bp"]) for row in exits)
    features = active.get("features") if isinstance(active.get("features"), Mapping) else {}
    maker_fee_bp = _feature_float(features, "maker_fee_bp") or 0.0
    taker_fee_bp = _feature_float(features, "taker_fee_bp") or maker_fee_bp
    entry_fee_bp = max(0.0, maker_fee_bp)
    fee_by_type = {
        "tp1": maker_fee_bp,
        "mid_tp": maker_fee_bp,
        "full_tp": maker_fee_bp,
        "trail": maker_fee_bp,
        "sl": taker_fee_bp,
        "ttl_mark": taker_fee_bp,
    }
    exit_fee_bp = 0.0
    for row in exits:
        exit_fee_bp += float(row["qty_frac"]) * max(0.0, float(fee_by_type.get(str(row["exit_type"]), taker_fee_bp)))
    after_fee = weighted_pnl - entry_fee_bp - exit_fee_bp
    runner_exits = [row for row in exits if row.get("component") == "runner"]
    runner_exit_bp = float(runner_exits[-1].get("pnl_bp") or 0.0) if runner_exits else 0.0
    runner_peak_bp = price_bp(side, entry, runner_peak) or 0.0
    giveback_bp = max(0.0, runner_peak_bp - runner_exit_bp) if runner_exits else 0.0

    return {
        "tp_policy_id": policy.get("tp_policy_id"),
        "filled": True,
        "filled_ts": filled_ms,
        "tp1_touched": tp1_touched_flag,
        "mid_tp_touched": any(row.get("exit_type") == "mid_tp" for row in exits),
        "full_tp_touched": any(row.get("exit_type") == "full_tp" for row in exits),
        "trail_armed": trail_armed,
        "runner_exit_type": runner_exit_type,
        "path_end_reason": path_end_reason,
        "ambiguous_path": ambiguous_path,
        "ambiguous_stage": ambiguous_stage,
        "primary_promotion_eligible": not ambiguous_path,
        "stress_mode_result": "adverse_first" if ambiguous_path else None,
        "component_exits": exits,
        "policy_pnl_bp_before_fee": round(weighted_pnl, 4),
        "policy_pnl_bp_after_fee": round(after_fee, 4),
        "entry_fee_bp": round(entry_fee_bp, 4),
        "tp1_exit_fee_bp": round(maker_fee_bp, 4),
        "mid_exit_fee_bp": round(maker_fee_bp if policy.get("mid_qty_frac") else 0.0, 4),
        "full_exit_fee_bp": round(maker_fee_bp, 4),
        "runner_exit_fee_bp": round(maker_fee_bp if runner_exit_type == "trail" else taker_fee_bp if runner_exits else 0.0, 4),
        "total_fee_bp": round(entry_fee_bp + exit_fee_bp, 4),
        "mfe_bp": round(mfe_bp, 4),
        "mae_bp": round(mae_bp, 4),
        "runner_peak_bp": round(runner_peak_bp, 4),
        "runner_exit_bp": round(runner_exit_bp, 4),
        "giveback_bp": round(giveback_bp, 4),
        "mfe_capture_ratio": round((after_fee / mfe_bp) if mfe_bp > 0 else 0.0, 6),
    }


def build_outcomes(
    settings: Any,
    active: Mapping[str, Any],
    candles: Sequence[Any],
    *,
    force_terminal: bool = False,
    terminal_reason: str | None = None,
) -> list[dict[str, Any]] | None:
    baseline = _baseline_from_mapping(active) or baseline_snapshot(settings, active)
    if not baseline:
        return []
    raw_policies = active.get("tp_policy_definitions")
    policies = [dict(policy) for policy in raw_policies] if isinstance(raw_policies, list) else policy_definitions(settings, baseline)
    results: dict[str, dict[str, Any]] = {}
    for policy in policies:
        validate_policy(policy)
        result = simulate_policy(
            settings,
            active,
            candles,
            policy,
            force_terminal=force_terminal,
            terminal_reason=terminal_reason,
        )
        if result is None:
            return None
        results[str(policy.get("tp_policy_id"))] = result

    baseline_result = results.get("baseline")
    if not baseline_result:
        return None
    baseline_pnl = float(baseline_result.get("policy_pnl_bp_after_fee") or 0.0)
    baseline_tp1 = bool(baseline_result.get("tp1_touched"))
    tp1_mismatch_count = sum(1 for result in results.values() if bool(result.get("tp1_touched")) != baseline_tp1)

    actual_live_pnl = active.get("actual_live_pnl_bp_after_fee")
    baseline_drift = None
    if actual_live_pnl is not None:
        try:
            baseline_drift = baseline_pnl - float(actual_live_pnl)
        except (TypeError, ValueError):
            baseline_drift = None

    notional = 0.0
    try:
        notional = float(active.get("requested_notional_usdc") or active.get("raw_requested_notional_usdc") or 0.0)
    except (TypeError, ValueError):
        notional = 0.0

    events: list[dict[str, Any]] = []
    for policy in policies:
        policy_id = str(policy.get("tp_policy_id"))
        result = results[policy_id]
        policy_pnl = float(result.get("policy_pnl_bp_after_fee") or 0.0)
        delta = policy_pnl - baseline_pnl
        prices = policy_prices(active, policy)
        outcome_id = "tpout_" + stable_hash(
            str(active.get("tp_policy_path_id")),
            policy_id,
            str(active.get("baseline_order_plan_hash")),
        )
        details = {
            "event_type": "tp_policy_shadow_outcome",
            "version": CODEX_TP_POLICY_VERSION,
            "source_type": active.get("source_type"),
            "paired_sample_id": active.get("paired_sample_id"),
            "tp_policy_path_id": active.get("tp_policy_path_id"),
            "tp_policy_outcome_id": outcome_id,
            "run_id": active.get("run_id"),
            "symbol": active.get("symbol"),
            "lane_family": active.get("shadow_lane_family") or active.get("lane_code"),
            "candidate_lane": active.get("candidate_lane"),
            "shadow_lane": active.get("shadow_lane"),
            "shadow_lane_family": active.get("shadow_lane_family"),
            "side": active.get("side"),
            "strategy": active.get("strategy"),
            "fill_model": active.get("fill_model"),
            "tp_policy_cohort_id": active.get("tp_policy_cohort_id"),
            "tp_policy_id": policy_id,
            "baseline_policy_id": "baseline",
            "entry_price": active.get("entry_price"),
            "tp1_price": round(float(prices["tp1"]), 8) if prices.get("tp1") else None,
            "mid_tp_price": round(float(prices["mid"]), 8) if prices.get("mid") else None,
            "full_tp_price": round(float(prices["full"]), 8) if prices.get("full") else None,
            "sl_price": active.get("sl_price"),
            "tp1_qty_frac": policy.get("tp1_qty_frac"),
            "mid_qty_frac": policy.get("mid_qty_frac"),
            "full_tp_qty_frac": policy.get("full_tp_qty_frac"),
            "runner_qty_frac": policy.get("runner_qty_frac"),
            "baseline_order_plan_hash": active.get("baseline_order_plan_hash"),
            "baseline_tp1_qty_frac": active.get("baseline_tp1_qty_frac"),
            "baseline_mid_qty_frac": active.get("baseline_mid_qty_frac"),
            "baseline_full_tp_qty_frac": active.get("baseline_full_tp_qty_frac"),
            "baseline_runner_qty_frac": active.get("baseline_runner_qty_frac"),
            "baseline_trail_arm_frac": active.get("baseline_trail_arm_frac"),
            "baseline_trail_giveback_frac": active.get("baseline_trail_giveback_frac"),
            "policy_pnl_bp_before_fee": result.get("policy_pnl_bp_before_fee"),
            "policy_pnl_bp_after_fee": result.get("policy_pnl_bp_after_fee"),
            "baseline_pnl_bp_after_fee": round(baseline_pnl, 4),
            "delta_vs_baseline_bp_after_fee": round(delta, 4),
            "delta_vs_baseline_usdc": round(delta / 10_000.0 * max(0.0, notional), 6),
            "beats_baseline": delta > 0,
            "tp1_touch_mismatch_count": tp1_mismatch_count,
            "baseline_simulator_drift_bp": round(baseline_drift, 4) if baseline_drift is not None else None,
            "actual_live_pnl_bp_after_fee": active.get("actual_live_pnl_bp_after_fee"),
            "actual_live_pnl_usdc_after_fee": active.get("actual_live_pnl_usdc_after_fee"),
            "actual_live_realized_pnl_usdc": active.get("actual_live_realized_pnl_usdc"),
            "actual_live_commission_usdc": active.get("actual_live_commission_usdc"),
            "terminalization_version": active.get("terminalization_version"),
            "terminalization_trigger": active.get("terminalization_trigger"),
            "path_quality": active.get("path_quality") or TP_POLICY_PATH_QUALITY,
            "policy_observation_ttl_s": active.get("policy_observation_ttl_s"),
            "live_tp_override_enabled": False,
            **result,
        }
        events.append(details)
    return events
