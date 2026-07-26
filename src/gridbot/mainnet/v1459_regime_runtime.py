"""Pure, bounded v1.4.59 regime overlay runtime.

This module deliberately does not wire itself into ``settings`` or ``one_run``.
It turns the existing detailed market-state labels into replayable evidence for
the shared regime FSM, and applies the resulting action only when explicitly
asked to run in enforcement mode.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import Any, Literal, Mapping

from src.gridbot.strategy.codex_v1_live import CodexV1Decision
from src.gridbot.strategy.live_next.regime_state import (
    Regime,
    RegimeConfig,
    RegimeDirection,
    RegimeEvidence,
    RegimeState,
    RegimeStateMachine,
)


V1459_POLICY_TAG = "v1.4.59_regime_overlay"
OverlayMode = Literal["candidate-only", "enforcement"]


class V1459RegimeState(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    SHOCK = "SHOCK"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True, slots=True)
class V1459ActionProfile:
    """Immutable action contract selected by the overlay."""

    name: str
    max_notional_usdc: float
    size_mult: float
    entry_offset_bp: float
    tp1_bp: float
    full_tp_bp: float
    partial_exit_pct: float
    sl_bp: float
    be_bp: float
    ttl_s: int
    hold_s: int
    maker_mode: str
    exit_mode: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("profile name must be a non-empty string")
        for field in (
            "max_notional_usdc",
            "size_mult",
            "entry_offset_bp",
            "tp1_bp",
            "full_tp_bp",
            "partial_exit_pct",
            "sl_bp",
            "be_bp",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
                raise ValueError(f"{field} must be a finite number")
            if float(value) < 0:
                raise ValueError(f"{field} must be non-negative")
        if self.max_notional_usdc <= 0:
            raise ValueError("max_notional_usdc must be positive")
        if self.partial_exit_pct > 1:
            raise ValueError("partial_exit_pct must be in [0, 1]")
        if self.full_tp_bp < self.tp1_bp:
            raise ValueError("full_tp_bp must be at least tp1_bp")
        for field in ("ttl_s", "hold_s"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        for field in ("maker_mode", "exit_mode"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field).strip():
                raise ValueError(f"{field} must be a non-empty string")


TREND_RUNNER = V1459ActionProfile(
    name="TREND_RUNNER",
    max_notional_usdc=50.0,
    size_mult=1.0,
    entry_offset_bp=2.0,
    tp1_bp=6.0,
    full_tp_bp=16.0,
    partial_exit_pct=0.70,
    sl_bp=10.0,
    be_bp=2.0,
    ttl_s=60,
    hold_s=720,
    maker_mode="ONE_STEP_REPRICE",
    exit_mode="RUNNER",
)

RANGE_SCALP = V1459ActionProfile(
    name="RANGE_SCALP",
    max_notional_usdc=50.0,
    size_mult=0.75,
    entry_offset_bp=1.0,
    tp1_bp=5.0,
    full_tp_bp=8.0,
    partial_exit_pct=1.0,
    sl_bp=8.0,
    be_bp=2.0,
    ttl_s=90,
    hold_s=360,
    maker_mode="PASSIVE",
    exit_mode="EARLY_FAIL",
)

SHOCK_RISK_OFF = V1459ActionProfile(
    name="SHOCK_RISK_OFF",
    max_notional_usdc=50.0,
    size_mult=0.0,
    entry_offset_bp=0.0,
    tp1_bp=0.0,
    full_tp_bp=0.0,
    partial_exit_pct=0.0,
    sl_bp=0.0,
    be_bp=0.0,
    ttl_s=0,
    hold_s=0,
    maker_mode="BLOCK",
    exit_mode="RISK_OFF",
)

INCUMBENT_FALLBACK = V1459ActionProfile(
    name="INCUMBENT_FALLBACK",
    max_notional_usdc=50.0,
    size_mult=1.0,
    entry_offset_bp=0.0,
    tp1_bp=0.0,
    full_tp_bp=0.0,
    partial_exit_pct=0.0,
    sl_bp=0.0,
    be_bp=0.0,
    ttl_s=0,
    hold_s=0,
    maker_mode="INCUMBENT",
    exit_mode="INCUMBENT",
)


@dataclass(frozen=True, slots=True)
class V1459RegimeConfig:
    """Tunable overlay bounds and profile selection."""

    trend_enter: float = 0.65
    trend_exit: float = 0.45
    range_enter: float = 0.65
    range_exit: float = 0.45
    shock_enter: float = 0.80
    shock_exit: float = 0.50
    switch_margin: float = 0.08
    direction_deadband: float = 0.05
    confirmations: int = 2
    min_dwell_ms: int = 15_000
    stale_after_ms: int = 90_000
    max_notional_usdc: float = 50.0
    trend_profile: V1459ActionProfile = TREND_RUNNER
    range_profile: V1459ActionProfile = RANGE_SCALP
    shock_profile: V1459ActionProfile = SHOCK_RISK_OFF
    fallback_profile: V1459ActionProfile = INCUMBENT_FALLBACK

    def __post_init__(self) -> None:
        if isinstance(self.max_notional_usdc, bool) or not isinstance(self.max_notional_usdc, (int, float)):
            raise ValueError("max_notional_usdc must be numeric")
        if not isfinite(float(self.max_notional_usdc)) or self.max_notional_usdc <= 0:
            raise ValueError("max_notional_usdc must be positive and finite")
        for profile_name in ("trend_profile", "range_profile", "shock_profile", "fallback_profile"):
            if not isinstance(getattr(self, profile_name), V1459ActionProfile):
                raise ValueError(f"{profile_name} must be V1459ActionProfile")
            if getattr(self, profile_name).max_notional_usdc > self.max_notional_usdc:
                raise ValueError(f"{profile_name} exceeds max_notional_usdc")

    def fsm_config(self) -> RegimeConfig:
        return RegimeConfig(
            trend_enter=self.trend_enter,
            trend_exit=self.trend_exit,
            range_enter=self.range_enter,
            range_exit=self.range_exit,
            shock_enter=self.shock_enter,
            shock_exit=self.shock_exit,
            switch_margin=self.switch_margin,
            direction_deadband=self.direction_deadband,
            confirmations=self.confirmations,
            min_dwell_ms=self.min_dwell_ms,
            stale_after_ms=self.stale_after_ms,
        )


_SUFFIX_TO_STATE = MappingProxyType(
    {
        "clean_extension": V1459RegimeState.TREND_UP,
        "hot_continuation": V1459RegimeState.TREND_UP,
        "strong_up_continuation": V1459RegimeState.TREND_UP,
        "ordinary_pullback_pre_vwap": V1459RegimeState.TREND_UP,
        "fast_reclaim": V1459RegimeState.TREND_UP,
        "strong_down_continuation": V1459RegimeState.TREND_DOWN,
        "falling_continuation_probe": V1459RegimeState.TREND_DOWN,
        "weak_chop": V1459RegimeState.RANGE,
        "mixed": V1459RegimeState.RANGE,
        "near_vwap_flat": V1459RegimeState.RANGE,
        "no_momentum_edge": V1459RegimeState.RANGE,
        "discount_mixed": V1459RegimeState.RANGE,
        "deep_discount_stable": V1459RegimeState.RANGE,
        "discount_delayed_reclaim": V1459RegimeState.RANGE,
        "stale_squeeze_top": V1459RegimeState.SHOCK,
        "counter_recoil": V1459RegimeState.SHOCK,
        "falling_discount_trap": V1459RegimeState.SHOCK,
    }
)


def map_market_state(market_state: str) -> V1459RegimeState:
    """Map a detailed incumbent label to the bounded overlay vocabulary."""

    if not isinstance(market_state, str) or not market_state.strip():
        return V1459RegimeState.UNCERTAIN
    normalized = market_state.strip().lower()
    if normalized in {state.value.lower() for state in V1459RegimeState}:
        return V1459RegimeState(normalized.upper())
    suffix = normalized.rsplit(":", 1)[-1].replace("-", "_")
    mapped = _SUFFIX_TO_STATE.get(suffix)
    if mapped is not None:
        return mapped
    if suffix in {"trend_up", "uptrend", "up_continuation"}:
        return V1459RegimeState.TREND_UP
    if suffix in {"trend_down", "downtrend", "down_continuation"}:
        return V1459RegimeState.TREND_DOWN
    if suffix in {"range", "ranging", "chop"}:
        return V1459RegimeState.RANGE
    if suffix in {"shock", "volatile", "volatility_shock"}:
        return V1459RegimeState.SHOCK
    return V1459RegimeState.UNCERTAIN


def _evidence_for(
    market_state: str,
    decision_time_ms: int,
    event_time_ms: int | None,
    available_at_ms: int | None,
) -> tuple[V1459RegimeState, RegimeEvidence]:
    mapped = map_market_state(market_state)
    event_time = decision_time_ms if event_time_ms is None else event_time_ms
    available_at = decision_time_ms if available_at_ms is None else available_at_ms
    scores = {
        V1459RegimeState.TREND_UP: (0.90, 0.0, 0.0, 0.90),
        V1459RegimeState.TREND_DOWN: (-0.90, 0.0, 0.0, -0.90),
        V1459RegimeState.RANGE: (0.0, 0.90, 0.0, 0.0),
        V1459RegimeState.SHOCK: (0.0, 0.0, 0.95, 0.0),
        V1459RegimeState.UNCERTAIN: (0.0, 0.0, 0.0, 0.0),
    }
    trend, range_score, shock, direction = scores[mapped]
    quality_flags = () if mapped is not V1459RegimeState.UNCERTAIN else ("invalid_data",)
    return mapped, RegimeEvidence(
        decision_time_ms=decision_time_ms,
        event_time_ms=event_time,
        available_at_ms=available_at,
        trend_score=trend,
        range_score=range_score,
        shock_score=shock,
        direction_score=direction,
        quality_flags=quality_flags,
    )


@dataclass(frozen=True, slots=True)
class V1459RegimeDecision:
    state: V1459RegimeState
    profile: V1459ActionProfile
    audit_payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.state, V1459RegimeState):
            raise TypeError("state must be V1459RegimeState")
        if not isinstance(self.profile, V1459ActionProfile):
            raise TypeError("profile must be V1459ActionProfile")
        object.__setattr__(self, "audit_payload", MappingProxyType(dict(self.audit_payload)))


def _state_from_fsm(state: RegimeState) -> V1459RegimeState:
    if state.regime is Regime.TREND and state.direction is RegimeDirection.UP:
        return V1459RegimeState.TREND_UP
    if state.regime is Regime.TREND and state.direction is RegimeDirection.DOWN:
        return V1459RegimeState.TREND_DOWN
    if state.regime is Regime.RANGE:
        return V1459RegimeState.RANGE
    if state.regime is Regime.SHOCK:
        return V1459RegimeState.SHOCK
    return V1459RegimeState.UNCERTAIN


def _profile_for(state: V1459RegimeState, config: V1459RegimeConfig) -> V1459ActionProfile:
    return {
        V1459RegimeState.TREND_UP: config.trend_profile,
        V1459RegimeState.TREND_DOWN: config.trend_profile,
        V1459RegimeState.RANGE: config.range_profile,
        V1459RegimeState.SHOCK: config.shock_profile,
        V1459RegimeState.UNCERTAIN: config.fallback_profile,
    }[state]


class V1459RegimeRuntime:
    """Stateful adapter whose only mutable state is the reused FSM instance."""

    def __init__(self, config: V1459RegimeConfig | None = None) -> None:
        self.config = config or V1459RegimeConfig()
        self._machine = RegimeStateMachine(self.config.fsm_config())

    @property
    def state(self) -> RegimeState | None:
        return self._machine.state

    def reset(self) -> None:
        self._machine.reset()

    def evaluate(
        self,
        market_state: str,
        *,
        decision_time_ms: int,
        event_time_ms: int | None = None,
        available_at_ms: int | None = None,
    ) -> V1459RegimeDecision:
        mapped, evidence = _evidence_for(
            market_state, decision_time_ms, event_time_ms, available_at_ms
        )
        fsm_state = self._machine.update(evidence)
        state = _state_from_fsm(fsm_state)
        profile = _profile_for(state, self.config)
        audit = {
            "overlay_version": "v1.4.59",
            "market_state": market_state,
            "mapped_state": mapped.value,
            "fsm_state": state.value,
            "fsm_direction": getattr(fsm_state.direction, "value", str(fsm_state.direction)),
            "fsm_reason": fsm_state.reason,
            "transition_count": fsm_state.transition_count,
            "pending_state": getattr(fsm_state.pending_regime, "value", None),
            "pending_count": fsm_state.pending_count,
            "decision_time_ms": decision_time_ms,
            "event_time_ms": evidence.event_time_ms,
            "available_at_ms": evidence.available_at_ms,
            "profile": profile.name,
        }
        return V1459RegimeDecision(state, profile, audit)


def evaluate_v1459_regime(
    market_state: str,
    *,
    decision_time_ms: int,
    runtime: V1459RegimeRuntime | None = None,
    event_time_ms: int | None = None,
    available_at_ms: int | None = None,
) -> V1459RegimeDecision:
    """Evaluate one observation, creating a runtime when no state is supplied."""

    active_runtime = runtime or V1459RegimeRuntime()
    return active_runtime.evaluate(
        market_state,
        decision_time_ms=decision_time_ms,
        event_time_ms=event_time_ms,
        available_at_ms=available_at_ms,
    )


def _annotated_metrics(
    decision: CodexV1Decision, overlay: V1459RegimeDecision, mode: OverlayMode
) -> dict[str, Any]:
    metrics = dict(decision.metrics) if isinstance(decision.metrics, Mapping) else {}
    metrics.update(
        {
            "v1459_regime_state": overlay.state.value,
            "v1459_regime_profile": overlay.profile.name,
            "v1459_regime_mode": mode,
            "v1459_regime_audit": dict(overlay.audit_payload),
        }
    )
    return metrics


def apply_v1459_regime_overlay(
    decision: CodexV1Decision,
    overlay: V1459RegimeDecision,
    *,
    mode: OverlayMode = "candidate-only",
) -> CodexV1Decision:
    """Purely annotate or enforce a v1.4.59 regime action profile."""

    if not isinstance(decision, CodexV1Decision):
        raise TypeError("decision must be CodexV1Decision")
    if not isinstance(overlay, V1459RegimeDecision):
        raise TypeError("overlay must be V1459RegimeDecision")
    if mode not in ("candidate-only", "enforcement"):
        raise ValueError("mode must be candidate-only or enforcement")

    metrics = _annotated_metrics(decision, overlay, mode)
    if mode == "candidate-only":
        return replace(decision, metrics=metrics)
    if overlay.state is V1459RegimeState.UNCERTAIN:
        return replace(decision, metrics=metrics)
    if overlay.state is V1459RegimeState.SHOCK:
        return replace(
            decision,
            accepted=False,
            entry_offset_bp=None,
            size_mult=0.0,
            notional_mult=0.0,
            requested_notional_usdc=0.0,
            reason=f"{V1459_POLICY_TAG}:shock_block",
            regime=overlay.state.value,
            risk_tags=tuple(dict.fromkeys((*decision.risk_tags, "v1459_shock_block"))),
            metrics=metrics,
            policy_tag=V1459_POLICY_TAG,
        )

    profile = overlay.profile
    requested_cap = min(profile.max_notional_usdc, profile.max_notional_usdc * profile.size_mult)
    metrics.update(
        {
            "entry_offset_bp": profile.entry_offset_bp,
            "size_mult": profile.size_mult,
            "notional_mult": profile.size_mult,
            "requested_notional_usdc": requested_cap,
            "requested_notional_cap_usdc": profile.max_notional_usdc,
            "applied_notional_cap_usdc": requested_cap,
            "tp1_bp": profile.tp1_bp,
            "full_tp_bp": profile.full_tp_bp,
            "partial_exit_pct": profile.partial_exit_pct,
            "sl_bp": profile.sl_bp,
            "be_bp": profile.be_bp,
            "ttl_s": profile.ttl_s,
            "hold_s": profile.hold_s,
            "maker_mode": profile.maker_mode,
            "exit_mode": profile.exit_mode,
        }
    )
    return replace(
        decision,
        entry_offset_bp=profile.entry_offset_bp,
        size_mult=profile.size_mult,
        notional_mult=profile.size_mult,
        requested_notional_usdc=requested_cap,
        regime=overlay.state.value,
        reason=f"{V1459_POLICY_TAG}:{profile.name.lower()}",
        metrics=metrics,
        policy_tag=V1459_POLICY_TAG,
    )


def apply_v1459_regime_decision(
    decision: CodexV1Decision,
    overlay: V1459RegimeDecision,
    *,
    mode: OverlayMode = "candidate-only",
) -> CodexV1Decision:
    return apply_v1459_regime_overlay(decision, overlay, mode=mode)


__all__ = [
    "INCUMBENT_FALLBACK",
    "RANGE_SCALP",
    "SHOCK_RISK_OFF",
    "TREND_RUNNER",
    "V1459ActionProfile",
    "V1459RegimeConfig",
    "V1459RegimeDecision",
    "V1459RegimeRuntime",
    "V1459RegimeState",
    "apply_v1459_regime_decision",
    "apply_v1459_regime_overlay",
    "evaluate_v1459_regime",
    "map_market_state",
]
