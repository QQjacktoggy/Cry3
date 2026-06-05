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
