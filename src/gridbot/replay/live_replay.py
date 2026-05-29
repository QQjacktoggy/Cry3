"""Execution-planning helpers for live-first replay backtests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from src.gridbot.strategy.long_pullback import Candle, SignalPlan
from src.gridbot.strategy.market_state import MarketStateDecision

ExecutionMode = Literal[
    "maker_pullback",
    "maker_micro",
    "marketable_momentum",
    "marketable_reclaim",
    "marketable_retest",
    "marketable_pullback",
    "marketable_vwap",
    "range_scalp",
    "skip",
]


@dataclass(frozen=True)
class ReplayConfig:
    legacy_5m_enabled: bool = True
    warmup_5m_bars: int = 288
    micro_enabled: bool = True
    micro_warmup_1m_bars: int = 80
    micro_lookback_bars: int = 18
    micro_volume_lookback_bars: int = 30
    micro_min_volume_ratio: float = 1.00
    micro_min_breakout_atr: float = 0.04
    micro_min_body_to_range: float = 0.35
    micro_max_extension_atr: float = 3.2
    micro_stop_atr: float = 1.15
    micro_take_profit_atr: float = 2.8
    micro_max_hold_minutes: int = 36
    micro_trade_cooldown_minutes: int = 30
    micro_take_profit_cooldown_minutes: int = 12
    micro_timeout_cooldown_minutes: int = 20
    micro_stop_cooldown_minutes: int = 90
    micro_margin_pct: float = 18.0
    micro_leverage_cap: float = 18.0
    micro_fixed_ticket_enabled: bool = False
    micro_target_net_profit_usdc: float = 0.75
    micro_max_loss_usdc: float = 1.25
    micro_min_ticket_notional_usdc: float = 10.0
    micro_entry_taker_fee_rate: float = 0.0004
    micro_maker_entry_fee_rate: float = 0.0002
    micro_take_profit_fee_rate: float = 0.0
    micro_stop_taker_fee_rate: float = 0.0004
    micro_maker_first_enabled: bool = False
    micro_maker_first_min_score: int = 58
    micro_maker_entry_atr: float = 0.10
    micro_maker_ttl_minutes: int = 3
    micro_maker_max_hold_minutes: int = 60
    micro_maker_first_strategies: tuple[str, ...] = (
        "micro_breakout_retest",
        "micro_ema_vwap_pullback",
        "micro_reclaim",
        "micro_vwap_reclaim",
    )
    micro_trend_filter_enabled: bool = True
    micro_trend_fast_bars: int = 34
    micro_trend_slow_bars: int = 80
    micro_trend_slope_lookback_bars: int = 20
    micro_trend_min_fast_slow_atr: float = 0.05
    micro_trend_min_slope_atr: float = 0.00
    micro_trend_close_floor_atr: float = 0.20
    micro_regime_v2_enabled: bool = True
    micro_regime_v2_ma_bars: int = 20
    micro_regime_v2_slope_bars: int = 3
    micro_regime_v2_recent_breakout_bars: int = 4
    micro_regime_v2_min_close_over_ma_atr: float = 0.08
    micro_regime_v2_min_ma_slope_atr: float = 0.03
    micro_regime_v2_min_breakout_atr: float = 0.12
    micro_regime_v2_breakout_max_age_bars: int = 1
    micro_regime_v2_breakout_min_volume_ratio: float = 1.05
    micro_regime_v2_breakout_min_body_to_range: float = 0.40
    micro_regime_v2_breakout_min_extension_atr: float = 0.05
    micro_regime_v2_breakout_max_extension_atr: float = 1.90
    micro_regime_v2_breakout_stop_atr: float = 0.90
    micro_regime_v2_breakout_take_profit_atr: float = 2.80
    micro_regime_v2_breakout_margin_pct: float = 16.0
    micro_regime_v2_breakout_leverage_cap: float = 18.0
    micro_regime_v2_pullback_max_age_bars: int = 4
    micro_regime_v2_pullback_touch_atr: float = 0.30
    micro_regime_v2_pullback_breakout_hold_atr: float = 0.10
    micro_regime_v2_pullback_higher_low_buffer_atr: float = 0.35
    micro_regime_v2_pullback_min_close_position: float = 0.58
    micro_regime_v2_pullback_min_volume_ratio: float = 0.70
    micro_regime_v2_pullback_stop_atr: float = 0.80
    micro_regime_v2_pullback_take_profit_atr: float = 1.90
    micro_regime_v2_pullback_margin_pct: float = 12.0
    micro_regime_v2_pullback_leverage_cap: float = 18.0
    micro_breakout_5m_impulse_filter_enabled: bool = True
    micro_breakout_5m_ma_bars: int = 20
    micro_breakout_5m_breakout_lookback_bars: int = 4
    micro_breakout_5m_ma_slope_bars: int = 3
    micro_breakout_5m_min_breakout_atr: float = 0.12
    micro_breakout_5m_min_close_over_ma_atr: float = 0.08
    micro_breakout_5m_min_ma_slope_atr: float = 0.03
    micro_structure_5m_filter_enabled: bool = True
    micro_structure_5m_fast_bars: int = 9
    micro_structure_5m_slow_bars: int = 21
    micro_structure_5m_slope_lookback_bars: int = 3
    micro_structure_5m_min_fast_slow_atr: float = 0.15
    micro_structure_5m_min_slope_atr: float = 0.05
    micro_structure_5m_close_floor_atr: float = 0.10
    micro_retest_enabled: bool = True
    micro_retest_lookback_bars: int = 3
    micro_retest_min_breakout_atr: float = 0.04
    micro_retest_breakout_min_volume_ratio: float = 0.85
    micro_retest_min_body_to_range: float = 0.30
    micro_retest_pullback_atr: float = 0.18
    micro_retest_reclaim_atr: float = 0.02
    micro_retest_min_close_position: float = 0.58
    micro_retest_min_volume_ratio: float = 0.60
    micro_retest_max_extension_atr: float = 1.60
    micro_retest_stop_atr: float = 0.75
    micro_retest_take_profit_atr: float = 1.70
    micro_retest_max_hold_minutes: int = 24
    micro_retest_margin_pct: float = 10.0
    micro_retest_leverage_cap: float = 18.0
    micro_pullback_enabled: bool = False
    micro_pullback_lookback_bars: int = 3
    micro_pullback_touch_buffer_atr: float = 0.20
    micro_pullback_min_close_position: float = 0.60
    micro_pullback_min_volume_ratio: float = 0.65
    micro_pullback_max_extension_atr: float = 1.20
    micro_pullback_slow_floor_atr: float = 0.35
    micro_pullback_require_recent_5m_breakout: bool = True
    micro_pullback_recent_5m_breakout_bars: int = 2
    micro_pullback_5m_higher_low_buffer_atr: float = 0.35
    micro_pullback_stop_atr: float = 0.70
    micro_pullback_take_profit_atr: float = 1.55
    micro_pullback_max_hold_minutes: int = 18
    micro_pullback_margin_pct: float = 8.0
    micro_pullback_leverage_cap: float = 14.0
    micro_reversion_enabled: bool = False
    micro_reversion_min_dip_atr: float = 0.85
    micro_reversion_min_close_position: float = 0.58
    micro_reversion_max_position_in_range: float = 0.55
    micro_reversion_min_volume_ratio: float = 0.75
    micro_reversion_stop_atr: float = 0.85
    micro_reversion_take_profit_atr: float = 2.6
    micro_reversion_max_hold_minutes: int = 18
    micro_reversion_margin_pct: float = 14.0
    micro_reversion_leverage_cap: float = 14.0
    micro_vwap_reclaim_enabled: bool = False
    micro_vwap_lookback_bars: int = 48
    micro_vwap_min_sweep_atr: float = 0.55
    micro_vwap_min_close_position: float = 0.62
    micro_vwap_min_volume_ratio: float = 0.80
    micro_vwap_max_extension_atr: float = 1.45
    micro_vwap_stop_atr: float = 0.80
    micro_vwap_take_profit_atr: float = 2.10
    micro_vwap_max_hold_minutes: int = 24
    micro_vwap_margin_pct: float = 12.0
    micro_vwap_leverage_cap: float = 18.0
    min_reward_pct: float = 0.18
    max_chase_gap_bps: float = 12.0
    stale_gap_bps: float = 25.0
    breakout_min_volume_ratio: float = 0.85
    breakout_max_extension_atr: float = 3.5
    maker_ttl_minutes: int = 25
    range_ttl_minutes: int = 25
    momentum_max_hold_minutes: int = 90
    maker_max_hold_minutes: int = 240
    range_max_hold_minutes: int = 90
    range_entry_atr: float = 0.18
    range_take_profit_atr: float = 0.32
    range_stop_atr: float = 0.85
    pending_preempt_score_buffer: int = 0


@dataclass(frozen=True)
class ExecutionPlan:
    mode: ExecutionMode
    side: Literal["long", "short"]
    entry_levels: tuple[float, ...]
    entry_weights: tuple[float, ...]
    stop_loss: float
    take_profit: float
    signal_price: float
    ttl_minutes: int
    max_hold_minutes: int
    score: int
    leverage_cap: float
    planned_notional_usdc: float
    market_gap_bps: float
    reason: str
    strategy: str
    playbook: str
    stale: bool = False
    risk_notes: tuple[str, ...] = field(default_factory=tuple)


def plan_execution(
    *,
    current_candle: Candle,
    market_decision: MarketStateDecision | None,
    breakout_signal: SignalPlan | None,
    pullback_signal: SignalPlan | None,
    config: ReplayConfig,
) -> ExecutionPlan | None:
    if market_decision is None:
        return None
    price = current_candle.close
    if breakout_signal is not None and breakout_signal.action == "PLAN_LONG" and _allow_breakout_followthrough(market_decision, config):
        return _plan_breakout(price, breakout_signal, market_decision, config)
    if market_decision.playbook == "long_pullback" and pullback_signal is not None and pullback_signal.action == "PLAN_LONG":
        return _plan_pullback(price, pullback_signal, market_decision, config)
    if market_decision.playbook == "vwap_reversion" and market_decision.risk_mode != "off":
        return _plan_range_scalp(current_candle, market_decision, config)
    return None


def plan_micro_execution(
    *,
    one_minute: list[Candle],
    config: ReplayConfig,
    equity_usdc: float,
) -> ExecutionPlan | None:
    if not config.micro_enabled or len(one_minute) < config.micro_warmup_1m_bars:
        return None
    current = one_minute[-1]
    atr = _atr(one_minute, 14)
    if atr is None or atr <= 0:
        return None
    recent = one_minute[-(config.micro_lookback_bars + 1):-1]
    volume_window = one_minute[-(config.micro_volume_lookback_bars + 1):-1]
    if len(recent) < config.micro_lookback_bars or len(volume_window) < config.micro_volume_lookback_bars:
        return None

    prior_high = max(candle.high for candle in recent)
    avg_volume = sum(candle.volume for candle in volume_window) / len(volume_window)
    if avg_volume <= 0:
        return None
    volume_ratio = current.volume / avg_volume
    candle_range = max(current.high - current.low, 0.0001)
    body_to_range = abs(current.close - current.open) / candle_range
    breakout_over_atr = (current.close - prior_high) / atr
    ema_fast = _ema([candle.close for candle in one_minute[-34:]], 13)
    ema_slow = _ema([candle.close for candle in one_minute[-80:]], 34)
    if ema_fast is None or ema_slow is None:
        return None
    extension_atr = (current.close - ema_fast) / atr
    vwap = _vwap(one_minute[-config.micro_vwap_lookback_bars:])
    if not _micro_trend_allows_long(
        one_minute=one_minute,
        current=current,
        atr=atr,
        config=config,
    ):
        return None
    structure_5m_ok = _micro_structure_5m_allows_long(one_minute=one_minute, config=config)
    regime_v2 = _classify_micro_regime_v2(
        one_minute=one_minute,
        current=current,
        atr=atr,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        volume_ratio=volume_ratio,
        config=config,
    )
    if config.micro_regime_v2_enabled and regime_v2 is not None:
        pullback_v2 = _plan_micro_trend_pullback_v2(
            one_minute=one_minute,
            current=current,
            atr=atr,
            volume_ratio=volume_ratio,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            vwap=vwap,
            regime=regime_v2,
            config=config,
            equity_usdc=equity_usdc,
        )
        if pullback_v2 is not None:
            return _finalize_micro_plan(pullback_v2, current=current, atr=atr, config=config)
        breakout_v2 = _plan_micro_breakout_v2(
            current=current,
            atr=atr,
            prior_high=prior_high,
            volume_ratio=volume_ratio,
            body_to_range=body_to_range,
            breakout_over_atr=breakout_over_atr,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            extension_atr=extension_atr,
            regime=regime_v2,
            config=config,
            equity_usdc=equity_usdc,
        )
        if breakout_v2 is not None:
            return _finalize_micro_plan(breakout_v2, current=current, atr=atr, config=config)

    breakout_plan = _plan_micro_breakout(
        one_minute=one_minute,
        current=current,
        atr=atr,
        prior_high=prior_high,
        volume_ratio=volume_ratio,
        body_to_range=body_to_range,
        breakout_over_atr=breakout_over_atr,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        extension_atr=extension_atr,
        config=config,
        equity_usdc=equity_usdc,
    )
    if breakout_plan is not None:
        return _finalize_micro_plan(breakout_plan, current=current, atr=atr, config=config)
    if not structure_5m_ok:
        return None
    retest_plan = _plan_micro_breakout_retest(
        one_minute=one_minute,
        current=current,
        atr=atr,
        volume_ratio=volume_ratio,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        extension_atr=extension_atr,
        config=config,
        equity_usdc=equity_usdc,
    )
    if retest_plan is not None:
        return _finalize_micro_plan(retest_plan, current=current, atr=atr, config=config)
    pullback_plan = _plan_micro_ema_vwap_pullback(
        one_minute=one_minute,
        current=current,
        atr=atr,
        volume_ratio=volume_ratio,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        vwap=vwap,
        config=config,
        equity_usdc=equity_usdc,
    )
    if pullback_plan is not None:
        return _finalize_micro_plan(pullback_plan, current=current, atr=atr, config=config)
    vwap_reclaim_plan = _plan_micro_vwap_reclaim(
        current=current,
        atr=atr,
        recent=recent,
        volume_ratio=volume_ratio,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        vwap=vwap,
        config=config,
        equity_usdc=equity_usdc,
    )
    if vwap_reclaim_plan is not None:
        return _finalize_micro_plan(vwap_reclaim_plan, current=current, atr=atr, config=config)
    reversion_plan = _plan_micro_reversion(
        current=current,
        atr=atr,
        recent=recent,
        volume_ratio=volume_ratio,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        config=config,
        equity_usdc=equity_usdc,
    )
    return _finalize_micro_plan(reversion_plan, current=current, atr=atr, config=config)


def _plan_micro_breakout(
    *,
    one_minute: list[Candle],
    current: Candle,
    atr: float,
    prior_high: float,
    volume_ratio: float,
    body_to_range: float,
    breakout_over_atr: float,
    ema_fast: float,
    ema_slow: float,
    extension_atr: float,
    config: ReplayConfig,
    equity_usdc: float,
) -> ExecutionPlan | None:
    if not _micro_breakout_5m_impulse_allows_long(one_minute=one_minute, config=config):
        return None
    if current.close <= current.open:
        return None
    if current.close <= prior_high + atr * config.micro_min_breakout_atr:
        return None
    if volume_ratio < config.micro_min_volume_ratio:
        return None
    if body_to_range < config.micro_min_body_to_range:
        return None
    if current.close <= ema_fast or ema_fast <= ema_slow:
        return None
    if extension_atr > config.micro_max_extension_atr:
        return None

    entry = current.close
    stop = entry - atr * config.micro_stop_atr
    take_profit = entry + atr * config.micro_take_profit_atr
    if reward_pct(entry, take_profit, "long") < config.min_reward_pct:
        return None

    score = 58
    score += min(int((volume_ratio - 1.0) * 18), 18)
    score += min(int(max(breakout_over_atr, 0.0) * 16), 16)
    score += min(int(body_to_range * 10), 8)
    score = min(score, 95)
    cap_notional = equity_usdc * config.micro_margin_pct / 100 * config.micro_leverage_cap
    planned_notional = _micro_planned_notional(entry, stop, take_profit, cap_notional, config)
    if planned_notional is None:
        return None
    return ExecutionPlan(
        mode="marketable_momentum",
        side="long",
        entry_levels=(round(entry, 4),),
        entry_weights=(1.0,),
        stop_loss=round(stop, 4),
        take_profit=round(take_profit, 4),
        signal_price=entry,
        ttl_minutes=1,
        max_hold_minutes=config.micro_max_hold_minutes,
        score=score,
        leverage_cap=config.micro_leverage_cap,
        planned_notional_usdc=planned_notional,
        market_gap_bps=0.0,
        reason="micro 1m breakout continuation",
        strategy="micro_breakout",
        playbook="micro_breakout",
        stale=False,
        risk_notes=(
            f"volume_ratio={volume_ratio:.2f}",
            f"breakout_atr={breakout_over_atr:.2f}",
            f"extension_atr={extension_atr:.2f}",
        ),
    )


def _plan_micro_breakout_v2(
    *,
    current: Candle,
    atr: float,
    prior_high: float,
    volume_ratio: float,
    body_to_range: float,
    breakout_over_atr: float,
    ema_fast: float,
    ema_slow: float,
    extension_atr: float,
    regime: dict[str, float | int | str],
    config: ReplayConfig,
    equity_usdc: float,
) -> ExecutionPlan | None:
    breakout_age = int(regime.get("breakout_age_bars", 99))
    regime_name = str(regime.get("regime", "no_trade"))
    breakout_level = float(regime.get("breakout_level", prior_high))
    if regime_name not in {"trend", "post_breakout"}:
        return None
    if breakout_age > config.micro_regime_v2_breakout_max_age_bars:
        return None
    if current.close <= current.open:
        return None
    if current.close <= prior_high + atr * config.micro_min_breakout_atr:
        return None
    if current.close <= breakout_level:
        return None
    if volume_ratio < config.micro_regime_v2_breakout_min_volume_ratio:
        return None
    if body_to_range < config.micro_regime_v2_breakout_min_body_to_range:
        return None
    if breakout_over_atr < config.micro_regime_v2_min_breakout_atr:
        return None
    if extension_atr < config.micro_regime_v2_breakout_min_extension_atr:
        return None
    if extension_atr > config.micro_regime_v2_breakout_max_extension_atr:
        return None
    if current.close <= ema_fast or ema_fast <= ema_slow:
        return None

    entry = current.close
    stop = entry - atr * config.micro_regime_v2_breakout_stop_atr
    take_profit = entry + atr * config.micro_regime_v2_breakout_take_profit_atr
    if reward_pct(entry, take_profit, "long") < config.min_reward_pct:
        return None

    score = 62
    score += min(int((volume_ratio - 1.0) * 16), 14)
    score += min(int(max(breakout_over_atr, 0.0) * 14), 12)
    score += min(int(body_to_range * 10), 8)
    score = min(score, 96)
    cap_notional = equity_usdc * config.micro_regime_v2_breakout_margin_pct / 100 * config.micro_regime_v2_breakout_leverage_cap
    planned_notional = _micro_planned_notional(entry, stop, take_profit, cap_notional, config)
    if planned_notional is None:
        return None
    return ExecutionPlan(
        mode="marketable_momentum",
        side="long",
        entry_levels=(round(entry, 4),),
        entry_weights=(1.0,),
        stop_loss=round(stop, 4),
        take_profit=round(take_profit, 4),
        signal_price=entry,
        ttl_minutes=1,
        max_hold_minutes=config.micro_max_hold_minutes,
        score=score,
        leverage_cap=config.micro_regime_v2_breakout_leverage_cap,
        planned_notional_usdc=planned_notional,
        market_gap_bps=0.0,
        reason="micro v2 breakout follow-through",
        strategy="micro_v2_breakout",
        playbook="micro_regime_v2",
        stale=False,
        risk_notes=(
            f"regime={regime_name}",
            f"breakout_age_bars={breakout_age}",
            f"volume_ratio={volume_ratio:.2f}",
            f"breakout_atr={breakout_over_atr:.2f}",
            f"extension_atr={extension_atr:.2f}",
        ),
    )


def _finalize_micro_plan(
    plan: ExecutionPlan | None,
    *,
    current: Candle,
    atr: float,
    config: ReplayConfig,
) -> ExecutionPlan | None:
    if plan is None:
        return None
    if not config.micro_maker_first_enabled:
        return plan
    if plan.strategy not in config.micro_maker_first_strategies:
        return plan
    if not plan.strategy.startswith("micro_") or plan.score < config.micro_maker_first_min_score:
        return None
    return _convert_to_micro_maker_plan(plan, current=current, atr=atr, config=config)


def _convert_to_micro_maker_plan(
    plan: ExecutionPlan,
    *,
    current: Candle,
    atr: float,
    config: ReplayConfig,
) -> ExecutionPlan | None:
    original_entry = plan.entry_levels[0]
    maker_entry = min(original_entry, current.close - atr * config.micro_maker_entry_atr)
    if maker_entry <= 0 or maker_entry <= plan.stop_loss:
        return None
    stop, take_profit = reanchor_bracket(
        side=plan.side,
        original_entry=original_entry,
        original_stop=plan.stop_loss,
        original_take_profit=plan.take_profit,
        executed_entry=maker_entry,
    )
    if reward_pct(maker_entry, take_profit, plan.side) < config.min_reward_pct:
        return None
    planned_notional = _micro_planned_notional(
        maker_entry,
        stop,
        take_profit,
        plan.planned_notional_usdc,
        config,
        entry_fee_rate=config.micro_maker_entry_fee_rate,
        take_profit_fee_rate=config.micro_take_profit_fee_rate,
        stop_fee_rate=config.micro_stop_taker_fee_rate,
    )
    if planned_notional is None:
        return None
    return ExecutionPlan(
        mode="maker_micro",
        side=plan.side,
        entry_levels=(round(maker_entry, 4),),
        entry_weights=(1.0,),
        stop_loss=stop,
        take_profit=take_profit,
        signal_price=current.close,
        ttl_minutes=config.micro_maker_ttl_minutes,
        max_hold_minutes=min(plan.max_hold_minutes, config.micro_maker_max_hold_minutes),
        score=plan.score,
        leverage_cap=plan.leverage_cap,
        planned_notional_usdc=planned_notional,
        market_gap_bps=round(market_gap_bps(plan.side, current.close, maker_entry), 3),
        reason=f"maker-first {plan.reason}",
        strategy=f"maker_{plan.strategy}",
        playbook=plan.playbook,
        stale=False,
        risk_notes=(
            *plan.risk_notes,
            f"maker_entry_atr={config.micro_maker_entry_atr:.2f}",
            f"source_mode={plan.mode}",
        ),
    )


def _plan_micro_breakout_retest(
    *,
    one_minute: list[Candle],
    current: Candle,
    atr: float,
    volume_ratio: float,
    ema_fast: float,
    ema_slow: float,
    extension_atr: float,
    config: ReplayConfig,
    equity_usdc: float,
) -> ExecutionPlan | None:
    if not config.micro_retest_enabled:
        return None
    if ema_fast <= ema_slow or current.close <= ema_fast:
        return None
    if extension_atr > config.micro_retest_max_extension_atr:
        return None
    if volume_ratio < config.micro_retest_min_volume_ratio:
        return None
    candle_range = max(current.high - current.low, 0.0001)
    close_position = (current.close - current.low) / candle_range
    if close_position < config.micro_retest_min_close_position:
        return None

    breakout = _find_recent_micro_breakout(one_minute, atr, config)
    if breakout is None:
        return None
    breakout_level, breakout_over_atr, breakout_volume_ratio = breakout
    if current.low > breakout_level + atr * config.micro_retest_pullback_atr:
        return None
    if current.close < breakout_level + atr * config.micro_retest_reclaim_atr:
        return None

    entry = current.close
    stop = min(current.low - atr * 0.12, entry - atr * config.micro_retest_stop_atr)
    take_profit = entry + atr * config.micro_retest_take_profit_atr
    if reward_pct(entry, take_profit, "long") < config.min_reward_pct:
        return None

    score = 59
    score += min(int(max(breakout_over_atr, 0.0) * 16), 14)
    score += min(int((breakout_volume_ratio - 0.8) * 12), 10)
    score += min(int(close_position * 10), 8)
    score = min(score, 92)
    cap_notional = equity_usdc * config.micro_retest_margin_pct / 100 * config.micro_retest_leverage_cap
    planned_notional = _micro_planned_notional(entry, stop, take_profit, cap_notional, config)
    if planned_notional is None:
        return None
    return ExecutionPlan(
        mode="marketable_retest",
        side="long",
        entry_levels=(round(entry, 4),),
        entry_weights=(1.0,),
        stop_loss=round(stop, 4),
        take_profit=round(take_profit, 4),
        signal_price=entry,
        ttl_minutes=1,
        max_hold_minutes=config.micro_retest_max_hold_minutes,
        score=score,
        leverage_cap=config.micro_retest_leverage_cap,
        planned_notional_usdc=planned_notional,
        market_gap_bps=0.0,
        reason="micro 1m breakout retest reclaim",
        strategy="micro_breakout_retest",
        playbook="micro_breakout_retest",
        stale=False,
        risk_notes=(
            f"breakout_level={breakout_level:.4f}",
            f"breakout_atr={breakout_over_atr:.2f}",
            f"volume_ratio={volume_ratio:.2f}",
        ),
    )


def _plan_micro_trend_pullback_v2(
    *,
    one_minute: list[Candle],
    current: Candle,
    atr: float,
    volume_ratio: float,
    ema_fast: float,
    ema_slow: float,
    vwap: float | None,
    regime: dict[str, float | int | str],
    config: ReplayConfig,
    equity_usdc: float,
) -> ExecutionPlan | None:
    regime_name = str(regime.get("regime", "no_trade"))
    breakout_age = int(regime.get("breakout_age_bars", 99))
    if regime_name != "post_breakout":
        return None
    if breakout_age > config.micro_regime_v2_pullback_max_age_bars:
        return None
    if current.close < current.open:
        return None
    if current.close < ema_fast or ema_fast <= ema_slow:
        return None
    if volume_ratio < config.micro_regime_v2_pullback_min_volume_ratio:
        return None

    breakout_level = float(regime.get("breakout_level", current.close))
    breakout_low = float(regime.get("breakout_low", current.low))
    recent = one_minute[-(config.micro_pullback_lookback_bars + 1):]
    if len(recent) < config.micro_pullback_lookback_bars + 1:
        return None
    recent_low = min(candle.low for candle in recent)
    anchor = ema_fast if vwap is None else max(min(ema_fast, vwap), ema_fast - atr * 0.25)
    if recent_low > anchor + atr * config.micro_regime_v2_pullback_touch_atr:
        return None
    if current.close < breakout_level - atr * config.micro_regime_v2_pullback_breakout_hold_atr:
        return None
    if recent_low < breakout_low - atr * config.micro_regime_v2_pullback_higher_low_buffer_atr:
        return None
    if current.low < ema_slow - atr * 0.25:
        return None
    candle_range = max(current.high - current.low, 0.0001)
    close_position = (current.close - current.low) / candle_range
    if close_position < config.micro_regime_v2_pullback_min_close_position:
        return None

    entry = current.close
    stop = min(recent_low - atr * 0.12, entry - atr * config.micro_regime_v2_pullback_stop_atr)
    take_profit = entry + atr * config.micro_regime_v2_pullback_take_profit_atr
    if reward_pct(entry, take_profit, "long") < config.min_reward_pct:
        return None

    score = 60
    score += min(int(close_position * 10), 8)
    score += min(int((volume_ratio - 0.6) * 12), 10)
    score += min(int(max(current.close - breakout_level, 0.0) / atr * 6), 8)
    score = min(score, 94)
    cap_notional = equity_usdc * config.micro_regime_v2_pullback_margin_pct / 100 * config.micro_regime_v2_pullback_leverage_cap
    planned_notional = _micro_planned_notional(entry, stop, take_profit, cap_notional, config)
    if planned_notional is None:
        return None
    return ExecutionPlan(
        mode="marketable_pullback",
        side="long",
        entry_levels=(round(entry, 4),),
        entry_weights=(1.0,),
        stop_loss=round(stop, 4),
        take_profit=round(take_profit, 4),
        signal_price=entry,
        ttl_minutes=1,
        max_hold_minutes=config.micro_pullback_max_hold_minutes,
        score=score,
        leverage_cap=config.micro_regime_v2_pullback_leverage_cap,
        planned_notional_usdc=planned_notional,
        market_gap_bps=0.0,
        reason="micro v2 post-breakout pullback continuation",
        strategy="micro_v2_trend_pullback",
        playbook="micro_regime_v2",
        stale=False,
        risk_notes=(
            f"breakout_age_bars={breakout_age}",
            f"breakout_level={breakout_level:.4f}",
            f"close_position={close_position:.2f}",
            f"volume_ratio={volume_ratio:.2f}",
        ),
    )


def _find_recent_micro_breakout(
    one_minute: list[Candle],
    atr: float,
    config: ReplayConfig,
) -> tuple[float, float, float] | None:
    max_offset = min(config.micro_retest_lookback_bars, len(one_minute) - config.micro_lookback_bars - 1)
    for offset in range(1, max_offset + 1):
        breakout_index = len(one_minute) - 1 - offset
        breakout = one_minute[breakout_index]
        lookback_start = breakout_index - config.micro_lookback_bars
        volume_start = breakout_index - config.micro_volume_lookback_bars
        if lookback_start < 0 or volume_start < 0:
            continue
        prior_window = one_minute[lookback_start:breakout_index]
        volume_window = one_minute[volume_start:breakout_index]
        avg_volume = sum(candle.volume for candle in volume_window) / len(volume_window)
        if avg_volume <= 0:
            continue
        prior_high = max(candle.high for candle in prior_window)
        breakout_range = max(breakout.high - breakout.low, 0.0001)
        body_to_range = abs(breakout.close - breakout.open) / breakout_range
        breakout_over_atr = (breakout.close - prior_high) / atr
        breakout_volume_ratio = breakout.volume / avg_volume
        if breakout.close <= breakout.open:
            continue
        if breakout_over_atr < config.micro_retest_min_breakout_atr:
            continue
        if breakout_volume_ratio < config.micro_retest_breakout_min_volume_ratio:
            continue
        if body_to_range < config.micro_retest_min_body_to_range:
            continue
        return prior_high, breakout_over_atr, breakout_volume_ratio
    return None


def _plan_micro_ema_vwap_pullback(
    *,
    one_minute: list[Candle],
    current: Candle,
    atr: float,
    volume_ratio: float,
    ema_fast: float,
    ema_slow: float,
    vwap: float | None,
    config: ReplayConfig,
    equity_usdc: float,
) -> ExecutionPlan | None:
    if not config.micro_pullback_enabled:
        return None
    if ema_fast < ema_slow:
        return None
    if current.close < ema_fast:
        return None
    reclaim_floor = vwap - atr * 0.10 if vwap is not None else ema_slow
    if current.close < reclaim_floor:
        return None
    if current.close < current.open and current.close <= one_minute[-2].close:
        return None
    if volume_ratio < config.micro_pullback_min_volume_ratio:
        return None
    recent_breakout = _find_recent_5m_breakout_context(one_minute=one_minute, config=config)
    if config.micro_pullback_require_recent_5m_breakout and recent_breakout is None:
        return None

    recent = one_minute[-(config.micro_pullback_lookback_bars + 1):]
    if len(recent) < config.micro_pullback_lookback_bars + 1:
        return None
    anchor = ema_fast if vwap is None else max(min(ema_fast, vwap), ema_fast - atr * 0.30)
    recent_low = min(candle.low for candle in recent)
    if recent_breakout is not None:
        breakout_level, breakout_low = recent_breakout
        if current.close < breakout_level - atr * 0.05:
            return None
        if recent_low < breakout_low - atr * config.micro_pullback_5m_higher_low_buffer_atr:
            return None
    if recent_low > anchor + atr * config.micro_pullback_touch_buffer_atr:
        return None
    if current.low < ema_slow - atr * config.micro_pullback_slow_floor_atr:
        return None
    extension_atr = (current.close - ema_fast) / atr
    if extension_atr > config.micro_pullback_max_extension_atr:
        return None
    candle_range = max(current.high - current.low, 0.0001)
    close_position = (current.close - current.low) / candle_range
    if close_position < config.micro_pullback_min_close_position:
        return None

    entry = current.close
    stop = min(recent_low - atr * 0.12, entry - atr * config.micro_pullback_stop_atr)
    take_profit = entry + atr * config.micro_pullback_take_profit_atr
    if reward_pct(entry, take_profit, "long") < config.min_reward_pct:
        return None

    score = 57
    score += min(int(close_position * 10), 8)
    score += min(int((volume_ratio - 0.6) * 12), 10)
    score += min(int(max(ema_fast - ema_slow, 0.0) / atr * 10), 12)
    score = min(score, 90)
    cap_notional = equity_usdc * config.micro_pullback_margin_pct / 100 * config.micro_pullback_leverage_cap
    planned_notional = _micro_planned_notional(entry, stop, take_profit, cap_notional, config)
    if planned_notional is None:
        return None
    return ExecutionPlan(
        mode="marketable_pullback",
        side="long",
        entry_levels=(round(entry, 4),),
        entry_weights=(1.0,),
        stop_loss=round(stop, 4),
        take_profit=round(take_profit, 4),
        signal_price=entry,
        ttl_minutes=1,
        max_hold_minutes=config.micro_pullback_max_hold_minutes,
        score=score,
        leverage_cap=config.micro_pullback_leverage_cap,
        planned_notional_usdc=planned_notional,
        market_gap_bps=0.0,
        reason="micro 1m EMA/VWAP pullback reclaim",
        strategy="micro_ema_vwap_pullback",
        playbook="micro_ema_vwap_pullback",
        stale=False,
        risk_notes=(
            f"close_position={close_position:.2f}",
            f"extension_atr={extension_atr:.2f}",
            f"volume_ratio={volume_ratio:.2f}",
        ),
    )


def _plan_micro_vwap_reclaim(
    *,
    current: Candle,
    atr: float,
    recent: list[Candle],
    volume_ratio: float,
    ema_fast: float,
    ema_slow: float,
    vwap: float | None,
    config: ReplayConfig,
    equity_usdc: float,
) -> ExecutionPlan | None:
    if not config.micro_vwap_reclaim_enabled or vwap is None:
        return None
    if current.close < current.open:
        return None
    if ema_fast < ema_slow and current.close < ema_fast:
        return None
    if volume_ratio < config.micro_vwap_min_volume_ratio:
        return None
    candle_range = max(current.high - current.low, 0.0001)
    close_position = (current.close - current.low) / candle_range
    if close_position < config.micro_vwap_min_close_position:
        return None
    reclaim_level = min(vwap, ema_fast)
    sweep_atr = (reclaim_level - current.low) / atr
    reclaim_atr = (current.close - reclaim_level) / atr
    extension_atr = (current.close - max(vwap, ema_fast)) / atr
    recent_low = min(candle.low for candle in recent)
    swept_recent_low = current.low <= recent_low + atr * 0.12
    if sweep_atr < config.micro_vwap_min_sweep_atr and not swept_recent_low:
        return None
    if reclaim_atr < 0:
        return None
    if extension_atr > config.micro_vwap_max_extension_atr:
        return None

    entry = current.close
    stop = min(current.low - atr * 0.15, entry - atr * config.micro_vwap_stop_atr)
    take_profit = entry + atr * config.micro_vwap_take_profit_atr
    if reward_pct(entry, take_profit, "long") < config.min_reward_pct:
        return None

    score = 57
    score += min(int(max(sweep_atr, 0.0) * 10), 16)
    score += min(int(close_position * 10), 8)
    score += min(int((volume_ratio - 0.75) * 10), 8)
    score = min(score, 90)
    cap_notional = equity_usdc * config.micro_vwap_margin_pct / 100 * config.micro_vwap_leverage_cap
    planned_notional = _micro_planned_notional(entry, stop, take_profit, cap_notional, config)
    if planned_notional is None:
        return None
    return ExecutionPlan(
        mode="marketable_vwap",
        side="long",
        entry_levels=(round(entry, 4),),
        entry_weights=(1.0,),
        stop_loss=round(stop, 4),
        take_profit=round(take_profit, 4),
        signal_price=entry,
        ttl_minutes=1,
        max_hold_minutes=config.micro_vwap_max_hold_minutes,
        score=score,
        leverage_cap=config.micro_vwap_leverage_cap,
        planned_notional_usdc=planned_notional,
        market_gap_bps=0.0,
        reason="micro 1m VWAP reclaim scalp",
        strategy="micro_vwap_reclaim",
        playbook="micro_vwap_reclaim",
        stale=False,
        risk_notes=(
            f"sweep_atr={sweep_atr:.2f}",
            f"reclaim_atr={reclaim_atr:.2f}",
            f"volume_ratio={volume_ratio:.2f}",
        ),
    )


def _plan_micro_reversion(
    *,
    current: Candle,
    atr: float,
    recent: list[Candle],
    volume_ratio: float,
    ema_fast: float,
    ema_slow: float,
    config: ReplayConfig,
    equity_usdc: float,
) -> ExecutionPlan | None:
    if not config.micro_reversion_enabled:
        return None
    if ema_fast < ema_slow and current.close < ema_slow:
        return None
    recent_low = min(candle.low for candle in recent)
    recent_high = max(candle.high for candle in recent)
    width = max(recent_high - recent_low, 0.0001)
    close_position = (current.close - current.low) / max(current.high - current.low, 0.0001)
    position_in_range = (current.close - recent_low) / width
    dip_atr = (ema_fast - current.low) / atr
    reclaim_atr = (current.close - current.low) / atr
    if dip_atr < config.micro_reversion_min_dip_atr:
        return None
    if close_position < config.micro_reversion_min_close_position:
        return None
    if position_in_range > config.micro_reversion_max_position_in_range:
        return None
    if current.close < current.open and reclaim_atr < config.micro_reversion_min_dip_atr * 0.55:
        return None
    if volume_ratio < config.micro_reversion_min_volume_ratio:
        return None

    entry = current.close
    stop = min(current.low - atr * 0.12, entry - atr * config.micro_reversion_stop_atr)
    take_profit = entry + atr * config.micro_reversion_take_profit_atr
    if reward_pct(entry, take_profit, "long") < config.min_reward_pct:
        return None

    score = 56
    score += min(int(dip_atr * 10), 18)
    score += min(int(close_position * 10), 8)
    score += min(int((volume_ratio - 0.7) * 10), 8)
    score = min(score, 90)
    cap_notional = equity_usdc * config.micro_reversion_margin_pct / 100 * config.micro_reversion_leverage_cap
    planned_notional = _micro_planned_notional(entry, stop, take_profit, cap_notional, config)
    if planned_notional is None:
        return None
    return ExecutionPlan(
        mode="marketable_reclaim",
        side="long",
        entry_levels=(round(entry, 4),),
        entry_weights=(1.0,),
        stop_loss=round(stop, 4),
        take_profit=round(take_profit, 4),
        signal_price=entry,
        ttl_minutes=1,
        max_hold_minutes=config.micro_reversion_max_hold_minutes,
        score=score,
        leverage_cap=config.micro_reversion_leverage_cap,
        planned_notional_usdc=planned_notional,
        market_gap_bps=0.0,
        reason="micro 1m dip reclaim scalp",
        strategy="micro_reclaim",
        playbook="micro_reclaim",
        stale=False,
        risk_notes=(
            f"dip_atr={dip_atr:.2f}",
            f"close_position={close_position:.2f}",
            f"volume_ratio={volume_ratio:.2f}",
        ),
    )


def reward_pct(entry: float, take_profit: float, side: Literal["long", "short"]) -> float:
    if entry <= 0 or take_profit <= 0:
        return 0.0
    distance = take_profit - entry if side == "long" else entry - take_profit
    return max(distance, 0.0) / entry * 100


def _micro_planned_notional(
    entry: float,
    stop: float,
    take_profit: float,
    cap_notional_usdc: float,
    config: ReplayConfig,
    *,
    entry_fee_rate: float | None = None,
    take_profit_fee_rate: float | None = None,
    stop_fee_rate: float | None = None,
) -> float | None:
    if not config.micro_fixed_ticket_enabled:
        return cap_notional_usdc
    if entry <= 0 or stop <= 0 or take_profit <= 0 or cap_notional_usdc <= 0:
        return None
    reward_fraction = max(take_profit - entry, 0.0) / entry
    risk_fraction = max(entry - stop, 0.0) / entry
    entry_fee = config.micro_entry_taker_fee_rate if entry_fee_rate is None else entry_fee_rate
    take_profit_fee = config.micro_take_profit_fee_rate if take_profit_fee_rate is None else take_profit_fee_rate
    stop_fee = config.micro_stop_taker_fee_rate if stop_fee_rate is None else stop_fee_rate
    net_profit_fraction = reward_fraction - entry_fee - take_profit_fee
    loss_fraction = risk_fraction + entry_fee + stop_fee
    if net_profit_fraction <= 0 or loss_fraction <= 0:
        return None

    target_notional = config.micro_target_net_profit_usdc / net_profit_fraction
    risk_notional = config.micro_max_loss_usdc / loss_fraction
    planned_notional = min(cap_notional_usdc, target_notional, risk_notional)
    if planned_notional < config.micro_min_ticket_notional_usdc:
        return None
    return planned_notional


def _atr(candles: list[Candle], period: int) -> float | None:
    if len(candles) <= period:
        return None
    true_ranges: list[float] = []
    for index in range(len(candles) - period, len(candles)):
        candle = candles[index]
        previous_close = candles[index - 1].close
        true_ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
    return sum(true_ranges) / len(true_ranges)


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    alpha = 2 / (period + 1)
    ema_value = sum(values[:period]) / period
    for value in values[period:]:
        ema_value = value * alpha + ema_value * (1 - alpha)
    return ema_value


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / len(window)


def _vwap(candles: list[Candle]) -> float | None:
    if not candles:
        return None
    total_volume = sum(candle.volume for candle in candles)
    if total_volume <= 0:
        return None
    price_volume = sum(((candle.high + candle.low + candle.close) / 3) * candle.volume for candle in candles)
    return price_volume / total_volume


def _micro_trend_allows_long(
    *,
    one_minute: list[Candle],
    current: Candle,
    atr: float,
    config: ReplayConfig,
) -> bool:
    if not config.micro_trend_filter_enabled:
        return True
    required = max(
        config.micro_trend_slow_bars,
        config.micro_trend_fast_bars + config.micro_trend_slope_lookback_bars,
    )
    if len(one_minute) < required:
        return False
    closes = [candle.close for candle in one_minute]
    trend_fast = _ema(closes[-config.micro_trend_slow_bars:], config.micro_trend_fast_bars)
    trend_slow = _ema(closes[-config.micro_trend_slow_bars:], config.micro_trend_slow_bars)
    prior_fast = _ema(closes[-required:-config.micro_trend_slope_lookback_bars], config.micro_trend_fast_bars)
    if trend_fast is None or trend_slow is None or prior_fast is None:
        return False
    fast_slow_atr = (trend_fast - trend_slow) / atr
    slope_atr = (trend_fast - prior_fast) / atr
    if fast_slow_atr < config.micro_trend_min_fast_slow_atr:
        return False
    if slope_atr < config.micro_trend_min_slope_atr:
        return False
    if current.close < trend_slow - atr * config.micro_trend_close_floor_atr:
        return False
    return True


def _micro_breakout_5m_impulse_allows_long(
    *,
    one_minute: list[Candle],
    config: ReplayConfig,
) -> bool:
    if not config.micro_breakout_5m_impulse_filter_enabled:
        return True
    metrics = _micro_5m_impulse_metrics(one_minute=one_minute, config=config, include_partial=True)
    if metrics is None:
        return True
    return (
        metrics["breakout_atr"] >= config.micro_breakout_5m_min_breakout_atr
        and metrics["close_over_ma_atr"] >= config.micro_breakout_5m_min_close_over_ma_atr
        and metrics["ma_slope_atr"] >= config.micro_breakout_5m_min_ma_slope_atr
    )


def _classify_micro_regime_v2(
    *,
    one_minute: list[Candle],
    current: Candle,
    atr: float,
    ema_fast: float,
    ema_slow: float,
    volume_ratio: float,
    config: ReplayConfig,
) -> dict[str, float | int | str] | None:
    if not config.micro_regime_v2_enabled:
        return None
    five_minute = _aggregate_completed_5m(one_minute)
    required = max(
        15,
        config.micro_regime_v2_ma_bars + config.micro_regime_v2_slope_bars,
        config.micro_breakout_5m_breakout_lookback_bars + config.micro_regime_v2_recent_breakout_bars,
    )
    if len(five_minute) < required:
        return None
    snapshot = _micro_5m_trend_snapshot(
        five_minute=five_minute,
        ma_bars=config.micro_regime_v2_ma_bars,
        slope_bars=config.micro_regime_v2_slope_bars,
    )
    if snapshot is None:
        return None
    if current.close < ema_fast or ema_fast <= ema_slow:
        return {"regime": "no_trade", **snapshot}
    if volume_ratio < 0.45:
        return {"regime": "no_trade", **snapshot}
    breakout = _find_recent_5m_impulse_breakout(five_minute=five_minute, config=config)
    if (
        snapshot["close_over_ma_atr"] < config.micro_regime_v2_min_close_over_ma_atr
        or snapshot["ma_slope_atr"] < config.micro_regime_v2_min_ma_slope_atr
    ):
        return {"regime": "no_trade", **snapshot}
    if breakout is not None:
        regime = "post_breakout" if breakout["breakout_age_bars"] <= config.micro_regime_v2_pullback_max_age_bars else "trend"
        return {"regime": regime, **snapshot, **breakout}
    return {"regime": "trend", **snapshot}


def _micro_5m_impulse_metrics(
    *,
    one_minute: list[Candle],
    config: ReplayConfig,
    include_partial: bool,
) -> dict[str, float] | None:
    five_minute = _aggregate_5m(one_minute, include_partial=include_partial)
    required = max(
        15,
        config.micro_breakout_5m_ma_bars + config.micro_breakout_5m_ma_slope_bars,
        config.micro_breakout_5m_breakout_lookback_bars + 1,
    )
    if len(five_minute) < required:
        return None
    atr_5m = _atr(five_minute, 14)
    if atr_5m is None or atr_5m <= 0:
        return None
    closes = [candle.close for candle in five_minute]
    ma_now = _sma(closes, config.micro_breakout_5m_ma_bars)
    ma_prior = _sma(
        closes[: -config.micro_breakout_5m_ma_slope_bars],
        config.micro_breakout_5m_ma_bars,
    )
    if ma_now is None or ma_prior is None:
        return None
    current = five_minute[-1]
    prior_window = five_minute[-(config.micro_breakout_5m_breakout_lookback_bars + 1) : -1]
    if len(prior_window) < config.micro_breakout_5m_breakout_lookback_bars:
        return None
    prior_high = max(candle.high for candle in prior_window)
    return {
        "breakout_atr": (current.close - prior_high) / atr_5m,
        "close_over_ma_atr": (current.close - ma_now) / atr_5m,
        "ma_slope_atr": (ma_now - ma_prior) / atr_5m,
        "breakout_level": prior_high,
        "breakout_low": current.low,
    }


def _micro_5m_trend_snapshot(
    *,
    five_minute: list[Candle],
    ma_bars: int,
    slope_bars: int,
) -> dict[str, float] | None:
    required = max(15, ma_bars + slope_bars)
    if len(five_minute) < required:
        return None
    closes = [candle.close for candle in five_minute]
    atr_5m = _atr(five_minute, 14)
    if atr_5m is None or atr_5m <= 0:
        return None
    ma_now = _sma(closes, ma_bars)
    ma_prior = _sma(closes[:-slope_bars], ma_bars)
    if ma_now is None or ma_prior is None:
        return None
    current = five_minute[-1]
    return {
        "atr_5m": atr_5m,
        "ma_now": ma_now,
        "close_over_ma_atr": (current.close - ma_now) / atr_5m,
        "ma_slope_atr": (ma_now - ma_prior) / atr_5m,
    }


def _find_recent_5m_impulse_breakout(
    *,
    five_minute: list[Candle],
    config: ReplayConfig,
) -> dict[str, float | int] | None:
    lookback = min(config.micro_regime_v2_recent_breakout_bars, len(five_minute))
    for age_bars in range(lookback):
        subset = five_minute[: len(five_minute) - age_bars]
        metrics = _micro_5m_impulse_metrics(
            one_minute=_expand_5m_to_1m_stub(subset),
            config=config,
            include_partial=True,
        )
        if metrics is None:
            continue
        if (
            metrics["breakout_atr"] >= config.micro_regime_v2_min_breakout_atr
            and metrics["close_over_ma_atr"] >= config.micro_regime_v2_min_close_over_ma_atr
            and metrics["ma_slope_atr"] >= config.micro_regime_v2_min_ma_slope_atr
        ):
            return {
                "breakout_level": metrics["breakout_level"],
                "breakout_low": metrics["breakout_low"],
                "breakout_age_bars": age_bars,
            }
    return None


def _micro_structure_5m_allows_long(
    *,
    one_minute: list[Candle],
    config: ReplayConfig,
) -> bool:
    if not config.micro_structure_5m_filter_enabled:
        return True
    five_minute = _aggregate_completed_5m(one_minute)
    required = max(
        config.micro_structure_5m_slow_bars,
        config.micro_structure_5m_fast_bars + config.micro_structure_5m_slope_lookback_bars,
        15,
    )
    if len(five_minute) < required:
        return True
    closes = [candle.close for candle in five_minute]
    atr_5m = _atr(five_minute, 14)
    if atr_5m is None or atr_5m <= 0:
        return True
    trend_fast = _ema(closes[-config.micro_structure_5m_slow_bars :], config.micro_structure_5m_fast_bars)
    trend_slow = _ema(closes[-config.micro_structure_5m_slow_bars :], config.micro_structure_5m_slow_bars)
    prior_fast = _ema(
        closes[-required : -config.micro_structure_5m_slope_lookback_bars],
        config.micro_structure_5m_fast_bars,
    )
    if trend_fast is None or trend_slow is None or prior_fast is None:
        return True
    fast_slow_atr = (trend_fast - trend_slow) / atr_5m
    slope_atr = (trend_fast - prior_fast) / atr_5m
    if fast_slow_atr < config.micro_structure_5m_min_fast_slow_atr:
        return False
    if slope_atr < config.micro_structure_5m_min_slope_atr:
        return False
    if five_minute[-1].close < trend_fast - atr_5m * config.micro_structure_5m_close_floor_atr:
        return False
    return True


def _aggregate_completed_5m(one_minute: list[Candle]) -> list[Candle]:
    return _aggregate_5m(one_minute, include_partial=False)


def _aggregate_5m(one_minute: list[Candle], *, include_partial: bool) -> list[Candle]:
    buckets: dict[int, list[Candle]] = {}
    for candle in one_minute:
        bucket = candle.open_time_ms - (candle.open_time_ms % (5 * 60_000))
        buckets.setdefault(bucket, []).append(candle)
    aggregated: list[Candle] = []
    for bucket, rows in sorted(buckets.items()):
        if len(rows) < 5 and not include_partial:
            continue
        ordered = sorted(rows, key=lambda item: item.open_time_ms)
        aggregated.append(
            Candle(
                open_time_ms=bucket,
                open=ordered[0].open,
                high=max(candle.high for candle in ordered),
                low=min(candle.low for candle in ordered),
                close=ordered[-1].close,
                volume=sum(candle.volume for candle in ordered),
                quote_volume=sum(getattr(candle, "quote_volume", 0.0) for candle in ordered),
            )
        )
    return aggregated


def _find_recent_5m_breakout_context(
    *,
    one_minute: list[Candle],
    config: ReplayConfig,
) -> tuple[float, float] | None:
    completed = _aggregate_completed_5m(one_minute)
    required = max(
        config.micro_breakout_5m_ma_bars + config.micro_breakout_5m_ma_slope_bars,
        config.micro_breakout_5m_breakout_lookback_bars + config.micro_pullback_recent_5m_breakout_bars,
        15,
    )
    if len(completed) < required:
        return None
    lookback = min(config.micro_pullback_recent_5m_breakout_bars, len(completed) - 1)
    for offset in range(1, lookback + 1):
        subset = completed[: len(completed) - offset + 1]
        metrics = _micro_5m_impulse_metrics(one_minute=_expand_5m_to_1m_stub(subset), config=config, include_partial=True)
        if metrics is None:
            continue
        if (
            metrics["breakout_atr"] >= config.micro_breakout_5m_min_breakout_atr
            and metrics["close_over_ma_atr"] >= config.micro_breakout_5m_min_close_over_ma_atr
            and metrics["ma_slope_atr"] >= config.micro_breakout_5m_min_ma_slope_atr
        ):
            return metrics["breakout_level"], metrics["breakout_low"]
    return None


def _expand_5m_to_1m_stub(five_minute: list[Candle]) -> list[Candle]:
    expanded: list[Candle] = []
    for candle in five_minute:
        expanded.append(
            Candle(
                open_time_ms=candle.open_time_ms,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                quote_volume=getattr(candle, "quote_volume", 0.0),
            )
        )
    return expanded


def reanchor_bracket(
    *,
    side: Literal["long", "short"],
    original_entry: float,
    original_stop: float,
    original_take_profit: float,
    executed_entry: float,
) -> tuple[float, float]:
    if side == "short":
        risk_distance = max(original_stop - original_entry, 0.0)
        reward_distance = max(original_entry - original_take_profit, 0.0)
        stop = executed_entry + risk_distance if risk_distance > 0 else executed_entry * 1.01
        take_profit = executed_entry - reward_distance if reward_distance > 0 else executed_entry * 0.992
    else:
        risk_distance = max(original_entry - original_stop, 0.0)
        reward_distance = max(original_take_profit - original_entry, 0.0)
        stop = executed_entry - risk_distance if risk_distance > 0 else executed_entry * 0.99
        take_profit = executed_entry + reward_distance if reward_distance > 0 else executed_entry * 1.008
    return round(stop, 4), round(take_profit, 4)


def market_gap_bps(side: Literal["long", "short"], market_price: float, entry_price: float) -> float:
    if entry_price <= 0:
        return 0.0
    if side == "short":
        return (entry_price - market_price) / entry_price * 10_000
    return (market_price - entry_price) / entry_price * 10_000


def should_preempt_pending(
    pending_plan: ExecutionPlan,
    candidate_plan: ExecutionPlan,
    config: ReplayConfig,
) -> bool:
    if candidate_plan.mode not in {"maker_micro", "marketable_momentum", "marketable_reclaim", "marketable_retest", "marketable_pullback", "marketable_vwap"}:
        return False
    if pending_plan.mode not in {"maker_micro", "maker_pullback", "range_scalp"}:
        return False
    return candidate_plan.score >= pending_plan.score + config.pending_preempt_score_buffer


def _allow_breakout_followthrough(
    market_decision: MarketStateDecision,
    config: ReplayConfig,
) -> bool:
    if market_decision.playbook == "long_breakout":
        return True
    if market_decision.trend == "down":
        return False
    if market_decision.breakout_quality != "strong":
        return False
    if market_decision.features.volume_ratio < config.breakout_min_volume_ratio:
        return False
    if market_decision.features.distance_to_ma20_atr > config.breakout_max_extension_atr:
        return False
    return True


def _plan_breakout(
    price: float,
    signal: SignalPlan,
    market_decision: MarketStateDecision,
    config: ReplayConfig,
) -> ExecutionPlan | None:
    if not signal.entries or signal.stop_loss is None or not signal.take_profits:
        return None
    original_entry = float(signal.entries[0])
    gap_bps = market_gap_bps("long", price, original_entry)
    stale = price >= float(signal.take_profits[0]) or gap_bps >= config.stale_gap_bps
    if stale:
        return None
    if gap_bps > config.max_chase_gap_bps:
        return None
    stop, take_profit = reanchor_bracket(
        side="long",
        original_entry=original_entry,
        original_stop=float(signal.stop_loss),
        original_take_profit=float(signal.take_profits[0]),
        executed_entry=price,
    )
    if reward_pct(price, take_profit, "long") < config.min_reward_pct:
        return None
    return ExecutionPlan(
        mode="marketable_momentum",
        side="long",
        entry_levels=(round(price, 4),),
        entry_weights=(1.0,),
        stop_loss=stop,
        take_profit=take_profit,
        signal_price=price,
        ttl_minutes=1,
        max_hold_minutes=config.momentum_max_hold_minutes,
        score=signal.score,
        leverage_cap=signal.leverage_cap,
        planned_notional_usdc=signal.planned_notional_usdc,
        market_gap_bps=round(gap_bps, 3),
        reason="breakout within chase gap",
        strategy="long_breakout",
        playbook=market_decision.playbook,
        stale=False,
        risk_notes=tuple(signal.risk_notes),
    )


def _plan_pullback(
    price: float,
    signal: SignalPlan,
    market_decision: MarketStateDecision,
    config: ReplayConfig,
) -> ExecutionPlan | None:
    if not signal.entries or signal.stop_loss is None or not signal.take_profits:
        return None
    entry = float(signal.entries[0])
    gap_bps = market_gap_bps("long", price, entry)
    stale = price >= float(signal.take_profits[0]) or gap_bps >= config.stale_gap_bps
    if reward_pct(entry, float(signal.take_profits[0]), "long") < config.min_reward_pct:
        return None
    return ExecutionPlan(
        mode="maker_pullback",
        side="long",
        entry_levels=tuple(round(level, 4) for level in signal.entries),
        entry_weights=tuple(float(weight) for weight in signal.entry_weights),
        stop_loss=round(float(signal.stop_loss), 4),
        take_profit=round(float(signal.take_profits[0]), 4),
        signal_price=price,
        ttl_minutes=config.maker_ttl_minutes,
        max_hold_minutes=config.maker_max_hold_minutes,
        score=signal.score,
        leverage_cap=signal.leverage_cap,
        planned_notional_usdc=signal.planned_notional_usdc,
        market_gap_bps=round(gap_bps, 3),
        reason="pullback maker ladder",
        strategy="long_pullback",
        playbook=market_decision.playbook,
        stale=stale,
        risk_notes=tuple(signal.risk_notes),
    )


def _plan_range_scalp(
    candle: Candle,
    market_decision: MarketStateDecision,
    config: ReplayConfig,
) -> ExecutionPlan | None:
    atr = market_decision.features.atr
    if atr <= 0:
        return None
    entry = candle.close - atr * config.range_entry_atr
    stop = entry - atr * config.range_stop_atr
    take_profit = entry + atr * config.range_take_profit_atr
    if reward_pct(entry, take_profit, "long") < config.min_reward_pct:
        return None
    return ExecutionPlan(
        mode="range_scalp",
        side="long",
        entry_levels=(round(entry, 4),),
        entry_weights=(1.0,),
        stop_loss=round(stop, 4),
        take_profit=round(take_profit, 4),
        signal_price=candle.close,
        ttl_minutes=config.range_ttl_minutes,
        max_hold_minutes=config.range_max_hold_minutes,
        score=int(round(market_decision.confidence * 100)),
        leverage_cap=8.0,
        planned_notional_usdc=40.0,
        market_gap_bps=round(market_gap_bps("long", candle.close, entry), 3),
        reason="range vwap reversion scalp",
        strategy="range_scalp",
        playbook=market_decision.playbook,
        stale=False,
        risk_notes=market_decision.reasons,
    )
