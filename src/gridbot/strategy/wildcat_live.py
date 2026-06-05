"""Live adapter for the wildcat S1-S5 research strategy.

The adapter intentionally imports the research primitives so live decisions do
not drift from the backtest while this strategy is still being validated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from scripts.backtest_wildcat_s1s5 import (
    build_candidates,
    build_features,
    candle_time,
    preset_params,
)
from src.gridbot.strategy.long_pullback import Candle, SignalPlan

TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class WildcatLiveDecision:
    signal: SignalPlan
    strategy: str
    side: str
    tp_pct: float
    sl_pct: float
    partial_exit_pct: float
    partial_tp_pct: float
    recovery_steps: int
    recovery_trigger_pct: float
    recovery_tp_shrink: float
    adverse_exit_bars: int
    adverse_exit_loss_pct: float
    max_holding_bars: int
    params_label: str


def _to_wildcat_candle(candle: Candle) -> dict:
    return {
        "time_ms": candle.open_time_ms,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
        "quote_volume": candle.quote_volume,
    }


def generate_wildcat_v2_adverse_guard_live_decision(
    candles: list[Candle],
    *,
    today_pnl_usdc: float = 0.0,
    today_peak_usdc: float | None = None,
    target_daily_usdc: float = 20.0,
    notional_usdc: float = 1000.0,
    leverage: int = 75,
) -> WildcatLiveDecision | None:
    """Return the latest wildcat decision using only available candles."""
    if len(candles) < 160:
        return None
    raw = [_to_wildcat_candle(c) for c in candles]
    params = preset_params(
        "wildcat_v2_adverse_guard",
        target_daily_usdc=target_daily_usdc,
        leverage_options=(leverage,),
    )
    features = build_features(raw)
    index = len(raw) - 1
    current_time = candle_time(raw[index])
    peak = today_pnl_usdc if today_peak_usdc is None else today_peak_usdc

    if params.daily_target_stop and today_pnl_usdc >= params.daily_profit_target_usdc:
        return None
    if (
        params.daily_target_stop
        and peak >= params.daily_floor_lock_usdc
        and today_pnl_usdc <= max(params.target_daily_usdc, peak - params.daily_giveback_usdc)
    ):
        return None

    catchup = (
        params.catchup_enabled
        and today_pnl_usdc < params.daily_profit_target_usdc
        and current_time.hour >= params.catchup_start_hour
    )
    rescue = (
        params.catchup_enabled
        and today_pnl_usdc < params.target_daily_usdc
        and current_time.hour >= params.rescue_hour
    )
    candidates = [
        row
        for row in build_candidates(raw, features, index, params, catchup=catchup, rescue=rescue)
        if row.strategy in params.enabled_strategies and row.score >= params.score_floor
    ]
    if not candidates:
        return None
    candidate = sorted(candidates, key=lambda row: row.score, reverse=True)[0]
    price = raw[index]["close"]
    if candidate.side == "LONG":
        action = "PLAN_LONG"
        stop = price * (1 - candidate.sl_pct)
        tp = price * (1 + candidate.tp_pct)
    else:
        action = "PLAN_SHORT"
        stop = price * (1 + candidate.sl_pct)
        tp = price * (1 - candidate.tp_pct)

    qty = notional_usdc / price if price > 0 else 0.0
    signal = SignalPlan(
        action=action,
        confidence=min(95, max(50, int(candidate.score))),
        score=int(candidate.score),
        symbol="ETHUSDC",
        price=price,
        rsi=features["rsi"][index],
        atr=features["atr"][index],
        support=features["bb_lower"][index] if candidate.side == "LONG" else features["bb_upper"][index],
        vwap=features["vwap"][index],
        entries=[price],
        entry_weights=[1.0],
        stop_loss=stop,
        take_profits=[tp],
        planned_notional_usdc=notional_usdc,
        planned_margin_usdc=notional_usdc / leverage,
        planned_qty=qty,
        leverage_cap=leverage,
        daily_target_usdc=(target_daily_usdc, target_daily_usdc),
        reasons=[
            f"wildcat:{candidate.strategy}",
            f"side:{candidate.side}",
            f"score:{candidate.score:.1f}",
            *candidate.reasons,
        ],
        risk_notes=[
            "mainnet_one_run",
            f"generated_at_taipei:{datetime.now(timezone.utc).astimezone(TAIPEI).strftime('%Y/%m/%d %H:%M:%S')}",
        ],
    )
    return WildcatLiveDecision(
        signal=signal,
        strategy=candidate.strategy,
        side=candidate.side,
        tp_pct=candidate.tp_pct,
        sl_pct=candidate.sl_pct,
        partial_exit_pct=params.partial_exit_pct,
        partial_tp_pct=params.partial_tp_pct,
        recovery_steps=params.recovery_steps,
        recovery_trigger_pct=params.recovery_trigger_pct,
        recovery_tp_shrink=params.recovery_tp_shrink,
        adverse_exit_bars=params.adverse_exit_bars,
        adverse_exit_loss_pct=params.adverse_exit_loss_pct,
        max_holding_bars=params.max_holding_bars,
        params_label=params.label,
    )


def explain_wildcat_no_signal(
    candles: list[Candle],
    *,
    today_pnl_usdc: float = 0.0,
    target_daily_usdc: float = 20.0,
    leverage: int = 75,
) -> list[str]:
    """Explain the reasons S1 and S5 did not trigger in wildcat v2 adverse guard."""
    if len(candles) < 160:
        return [f"資料不足：目前只有 {len(candles)} 根 K 線，至少需要 160 根"]

    raw = [_to_wildcat_candle(c) for c in candles]
    params = preset_params(
        "wildcat_v2_adverse_guard",
        target_daily_usdc=target_daily_usdc,
        leverage_options=(leverage,),
    )
    features = build_features(raw)
    index = len(raw) - 1
    c = raw[index]

    price = c["close"]
    atr_val = features["atr"][index] if features["atr"][index] and features["atr"][index] > 0 else max(c["high"] - c["low"], 1.0)
    vol_ratio = c["volume"] / features["volume_sma"][index] if features["volume_sma"][index] > 0 else 0.0
    body_ratio = abs(c["close"] - c["open"]) / (c["high"] - c["low"]) if c["high"] > c["low"] else 0.0

    from scripts.backtest_wildcat_s1s5 import volatility_state, s1_regime_allowed
    vol_state = volatility_state(features["atr_pct"][index])
    trend = features["trend"][index]
    rsi_val = features["rsi"][index]
    stoch_k_val = features["stoch_k"][index]
    stoch_d_val = features["stoch_d"][index]
    bb_lower_val = features["bb_lower"][index]
    bb_upper_val = features["bb_upper"][index]

    reasons = [
        "市場特徵："
        f"trend=<code>{trend}</code> / "
        f"vol=<code>{vol_state}</code> / "
        f"量能比=<code>{vol_ratio:.2f}</code> (門檻&gt;={params.min_vol_ratio:.2f}) / "
        f"實體比=<code>{body_ratio:.2f}</code> (門檻&gt;={params.strict_body_ratio:.2f})"
    ]

    # Check S1
    s1_allowed = s1_regime_allowed(features, index, params)
    s1_status = "inactive"
    s1_reasons = []
    if not s1_allowed:
        s1_reasons.append("RegimeGuard阻擋(趨勢佔比或均線擴張過大)")
    if trend != "range":
        s1_reasons.append(f"需要 range，目前為 {trend}")
    if vol_state not in {"low", "normal"}:
        s1_reasons.append(f"需要 low/normal 波動，目前為 {vol_state}")
    if vol_ratio < params.min_vol_ratio:
        s1_reasons.append(f"量能比例 {vol_ratio:.2f} &lt; {params.min_vol_ratio:.2f}")
    if body_ratio < params.strict_body_ratio:
        s1_reasons.append(f"實體比例 {body_ratio:.2f} &lt; {params.strict_body_ratio:.2f}")

    long_atr_bound = bb_lower_val + params.range_edge_atr_margin * atr_val
    short_atr_bound = bb_upper_val - params.range_edge_atr_margin * atr_val

    s1_long_ok = price <= long_atr_bound and rsi_val <= params.s1_rsi_long_max
    s1_short_ok = price >= short_atr_bound and rsi_val >= params.s1_rsi_short_min

    if not s1_reasons:
        s1_status = "watch"
        if not s1_long_ok and not s1_short_ok:
            s1_reasons.append(
                f"未達邊界；做多需價格&lt;=${long_atr_bound:.2f}(目前${price:.2f})且RSI&lt;={params.s1_rsi_long_max:.1f}(目前{rsi_val:.1f})；"
                f"做空需價格&gt;=${short_atr_bound:.2f}(目前${price:.2f})且RSI&gt;={params.s1_rsi_short_min:.1f}(目前{rsi_val:.1f})"
            )
        else:
            s1_status = "ready"

    reasons.append(
        f"<b>S1_BB_RSI [{s1_status}]</b>：RSI=<code>{rsi_val:.1f}</code>，布林=[<code>${bb_lower_val:.2f}</code>, <code>${bb_upper_val:.2f}</code>]"
        + (f"\n└ 未滿足：{', '.join(s1_reasons)}" if s1_reasons else " (已滿足)")
    )

    # Check S5
    s5_status = "inactive"
    s5_reasons = []
    if trend != "range":
        s5_reasons.append(f"需要 range，目前為 {trend}")
    if vol_state != "normal":
        s5_reasons.append(f"需要 normal 波動，目前為 {vol_state}")
    if vol_ratio < params.min_vol_ratio:
        s5_reasons.append(f"量能比例 {vol_ratio:.2f} &lt; {params.min_vol_ratio:.2f}")
    if body_ratio < params.strict_body_ratio:
        s5_reasons.append(f"實體比例 {body_ratio:.2f} &lt; {params.strict_body_ratio:.2f}")

    stoch_k_prev = features["stoch_k"][index - 1]
    stoch_d_prev = features["stoch_d"][index - 1]
    stoch_up = stoch_k_prev <= stoch_d_prev and stoch_k_val > stoch_d_val
    stoch_dn = stoch_k_prev >= stoch_d_prev and stoch_k_val < stoch_d_val

    vwap_val = features["vwap"][index]
    long_vwap_bound = vwap_val + (0.25 + params.range_edge_atr_margin) * atr_val
    short_vwap_bound = vwap_val - (0.25 + params.range_edge_atr_margin) * atr_val

    s5_long_ok = stoch_up and stoch_d_val < params.s5_long_d_max and price < long_vwap_bound
    s5_short_ok = stoch_dn and stoch_d_val > params.s5_short_d_min and price > short_vwap_bound

    if not s5_reasons:
        s5_status = "watch"
        if not s5_long_ok and not s5_short_ok:
            s5_reasons.append(
                f"未交叉或未達邊界；K={stoch_k_val:.1f}, D={stoch_d_val:.1f}；"
                f"做多需Stoch黃金交叉且D&lt;{params.s5_long_d_max:.1f}且價格&lt;=${long_vwap_bound:.2f}；"
                f"做空需Stoch死亡交叉且D&gt;{params.s5_short_d_min:.1f}且價格&gt;=${short_vwap_bound:.2f}"
            )
        else:
            s5_status = "ready"

    reasons.append(
        f"<b>S5_Stoch [{s5_status}]</b>：K/D=[<code>{stoch_k_val:.1f}</code>, <code>{stoch_d_val:.1f}</code>]，VWAP=<code>${vwap_val:.2f}</code>"
        + (f"\n└ 未滿足：{', '.join(s5_reasons)}" if s5_reasons else " (已滿足)")
    )

    return reasons
