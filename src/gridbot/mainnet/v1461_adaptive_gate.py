"""Pure v1.4.61 bidirectional regime-aware adaptive gate.

The policy is intentionally side-effect free.  Runtime code owns durable
evidence, episode tracking and atomic token consumption; this module validates
those facts and returns one closed action from the reviewed menu.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import Enum
import hashlib
import json
from math import isfinite
from types import MappingProxyType
from typing import Any, Literal, Mapping

from src.gridbot.strategy.codex_v1_live import CodexV1Decision


V1461_VERSION = "v1.4.61"
V1461_POLICY_NAME = "codex-v1.4.61-bidirectional-regime-gate"
OverlayMode = Literal["candidate-only", "enforcement"]


class AdaptiveActionMode(str, Enum):
    CONTROL = "CONTROL"
    FAST_PROBE_0_5 = "FAST_PROBE_0_5"
    PROBATION_0_5 = "PROBATION_0_5"
    SHADOW_BLOCK = "SHADOW_BLOCK"
    HARD_BLOCK = "HARD_BLOCK"
    HALT = "HALT"


class RegimeCompatibility(str, Enum):
    SUPPORTIVE = "SUPPORTIVE"
    NEUTRAL = "NEUTRAL"
    ADVERSE = "ADVERSE"
    HARD_BLOCK = "HARD_BLOCK"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class AdaptiveGateConfig:
    version: str = V1461_VERSION
    policy_name: str = V1461_POLICY_NAME
    compatibility_schema_version: str = "v1461.lane-compatibility.2-aggtrade-cost"
    execution_contract_hash: str = "standalone-test-contract"
    shadow_all_strategy_rejects_enabled: bool = True
    runner_enabled: bool = False
    one_step_reprice_enabled: bool = False
    max_notional_usdc: float = 50.0
    probation_notional_usdc: float = 25.0
    fast_min_evaluable: int = 4
    fast_min_tp_first: int = 3
    probation_min_evaluable: int = 6
    probation_min_tp_first: int = 4
    control_min_paid_complete: int = 3
    control_min_paid_wins: int = 2
    min_ev_per_opportunity_usdc: float = 0.0
    evidence_max_age_seconds: int = 6 * 60 * 60
    regime_confirmations: int = 2
    episode_exit_confirmation_seconds: int = 5 * 60
    lane_loss_streak_limit: int = 2
    lane_net_loss_cap_usdc: float = 0.12
    cohort_net_loss_cap_usdc: float = 0.30

    def __post_init__(self) -> None:
        if (
            not self.version.strip()
            or not self.policy_name.strip()
            or not self.compatibility_schema_version.strip()
            or not self.execution_contract_hash.strip()
        ):
            raise ValueError("policy identity strings must be non-empty")
        for name in (
            "shadow_all_strategy_rejects_enabled",
            "runner_enabled",
            "one_step_reprice_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")
        if self.runner_enabled or self.one_step_reprice_enabled:
            raise ValueError("runner and one-step reprice are closed in v1.4.61")
        for name in (
            "fast_min_evaluable",
            "fast_min_tp_first",
            "probation_min_evaluable",
            "probation_min_tp_first",
            "control_min_paid_complete",
            "control_min_paid_wins",
            "evidence_max_age_seconds",
            "regime_confirmations",
            "episode_exit_confirmation_seconds",
            "lane_loss_streak_limit",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.fast_min_evaluable < 4 or self.fast_min_tp_first < 3:
            raise ValueError("FAST_PROBE thresholds cannot be looser than 4/3")
        if self.probation_min_evaluable < 6 or self.probation_min_tp_first < 4:
            raise ValueError("PROBATION thresholds cannot be looser than 6/4")
        if self.fast_min_tp_first > self.fast_min_evaluable:
            raise ValueError("fast TP threshold exceeds evaluable threshold")
        if self.probation_min_tp_first > self.probation_min_evaluable:
            raise ValueError("probation TP threshold exceeds evaluable threshold")
        if self.control_min_paid_complete < 3 or self.control_min_paid_wins < 2:
            raise ValueError("CONTROL paid thresholds cannot be looser than 3/2")
        if self.control_min_paid_wins > self.control_min_paid_complete:
            raise ValueError("control wins cannot exceed paid completes")
        for name in (
            "max_notional_usdc",
            "probation_notional_usdc",
            "min_ev_per_opportunity_usdc",
            "lane_net_loss_cap_usdc",
            "cohort_net_loss_cap_usdc",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be finite")
            if not isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if not 0.0 < self.max_notional_usdc <= 50.0:
            raise ValueError("max_notional_usdc must be in (0, 50]")
        if not 0.0 < self.probation_notional_usdc <= 25.0:
            raise ValueError("probation_notional_usdc must be in (0, 25]")
        if self.min_ev_per_opportunity_usdc < 0.0:
            raise ValueError("EV threshold cannot be negative")
        if not 0.0 < self.lane_net_loss_cap_usdc <= 0.12:
            raise ValueError("lane loss cap must be in (0, 0.12]")
        if not 0.0 < self.cohort_net_loss_cap_usdc <= 0.30:
            raise ValueError("cohort loss cap must be in (0, 0.30]")
        if self.evidence_max_age_seconds <= 0:
            raise ValueError("evidence_max_age_seconds must be positive")
        if self.regime_confirmations < 2:
            raise ValueError("regime_confirmations must be at least 2")
        if self.episode_exit_confirmation_seconds < 5 * 60:
            raise ValueError("episode exit confirmation must be at least 5 minutes")

    @property
    def policy_hash(self) -> str:
        payload = {
            "config": asdict(self),
            "action_modes": [mode.value for mode in AdaptiveActionMode],
            "compatibility": [mode.value for mode in RegimeCompatibility],
            "comparators": {
                "fresh": "age_seconds<=max",
                "ev": "strictly_greater_than_threshold",
                "lane_loss": "<=-cap",
                "cohort_loss": "<=-cap",
                "last_fast_outcome": "not_sl_first",
            },
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class GateEvidence:
    opportunities: int = 0
    evaluable: int = 0
    tp_first: int = 0
    sl_first: int = 0
    max_hold: int = 0
    no_fill: int = 0
    ambiguous: int = 0
    incomplete: int = 0
    net_pnl_usdc: float = 0.0
    last_outcome: str | None = None
    last_outcome_at_ms: int | None = None
    matching_episode_count: int = 0
    first_probe_net_pnl_usdc: float | None = None
    first_probe_episode_id: str | None = None
    paid_complete: int = 0
    paid_wins: int = 0
    paid_net_pnl_usdc: float = 0.0
    paid_integrity_complete: bool = True

    def __post_init__(self) -> None:
        for name in (
            "opportunities", "evaluable", "tp_first", "sl_first", "max_hold", "no_fill",
            "ambiguous", "incomplete", "matching_episode_count", "paid_complete",
            "paid_wins",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.tp_first + self.sl_first + self.max_hold > self.evaluable:
            raise ValueError("first-touch outcomes exceed evaluable count")
        if self.paid_wins > self.paid_complete:
            raise ValueError("paid wins exceed paid completes")
        for name in ("net_pnl_usdc", "paid_net_pnl_usdc"):
            if not isfinite(float(getattr(self, name))):
                raise ValueError(f"{name} must be finite")
        if self.first_probe_net_pnl_usdc is not None and not isfinite(
            float(self.first_probe_net_pnl_usdc)
        ):
            raise ValueError("first_probe_net_pnl_usdc must be finite")
        if self.last_outcome_at_ms is not None and self.last_outcome_at_ms < 0:
            raise ValueError("last_outcome_at_ms must be non-negative")
        if not isinstance(self.paid_integrity_complete, bool):
            raise ValueError("paid_integrity_complete must be bool")

    @property
    def ev_per_opportunity_usdc(self) -> float:
        return self.net_pnl_usdc / self.opportunities if self.opportunities else 0.0


@dataclass(frozen=True, slots=True)
class AdaptiveGateRiskInput:
    integrity_safe: bool = True
    global_halted: bool = False
    key_quarantined: bool = False
    consecutive_paid_losses: int = 0
    key_net_pnl_usdc: float = 0.0
    cohort_net_pnl_usdc: float = 0.0


@dataclass(frozen=True, slots=True)
class AdaptiveGateDecision:
    action_mode: AdaptiveActionMode
    permits_order: bool
    incumbent_accepted: bool
    gate_family_id: str
    lane: str
    market_state: str
    episode_id: str
    matrix_rule_id: str
    risk_scale: float
    max_notional_usdc: float
    evidence_gate: Mapping[str, Any]
    token_id: str | None
    policy_hash: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_gate", MappingProxyType(dict(self.evidence_gate)))


def promotion_key(gate_family_id: str, lane: str, market_state: str) -> str:
    return "|".join(
        part.strip().upper().replace("|", "_") or "UNKNOWN"
        for part in (gate_family_id, lane, market_state)
    )


def promotion_token_id(
    policy_hash: str,
    gate_family_id: str,
    lane: str,
    market_state: str,
    episode_id: str,
) -> str:
    raw = "|".join(
        (policy_hash, promotion_key(gate_family_id, lane, market_state), episode_id)
    )
    return "v1461_" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def _decision(
    *,
    config: AdaptiveGateConfig,
    action: AdaptiveActionMode,
    incumbent_accepted: bool,
    gate_family_id: str,
    lane: str,
    market_state: str,
    episode_id: str,
    rule: str,
    reason: str,
    evidence_gate: Mapping[str, Any],
    token_id: str | None = None,
) -> AdaptiveGateDecision:
    permits = action in {
        AdaptiveActionMode.CONTROL,
        AdaptiveActionMode.FAST_PROBE_0_5,
        AdaptiveActionMode.PROBATION_0_5,
    }
    if action is AdaptiveActionMode.CONTROL:
        scale, cap = 1.0, config.max_notional_usdc
    elif permits:
        scale, cap = 0.5, config.probation_notional_usdc
    else:
        scale, cap = 0.0, 0.0
    return AdaptiveGateDecision(
        action_mode=action,
        permits_order=permits,
        incumbent_accepted=incumbent_accepted,
        gate_family_id=gate_family_id,
        lane=lane,
        market_state=market_state,
        episode_id=episode_id,
        matrix_rule_id=rule,
        risk_scale=scale,
        max_notional_usdc=cap,
        evidence_gate=evidence_gate,
        token_id=token_id,
        policy_hash=config.policy_hash,
        reason=reason,
    )


def select_adaptive_gate_decision(
    *,
    incumbent_accepted: bool,
    promotion_eligible: bool,
    gate_family_id: str,
    lane: str,
    market_state: str,
    episode_id: str,
    compatibility: RegimeCompatibility,
    evidence: GateEvidence | None = None,
    risk: AdaptiveGateRiskInput | None = None,
    now_ms: int,
    token_consumed: bool = False,
    config: AdaptiveGateConfig | None = None,
) -> AdaptiveGateDecision:
    """Select a closed bidirectional action without mutating runtime state."""

    active = config or AdaptiveGateConfig()
    facts = evidence or GateEvidence()
    risk_facts = risk or AdaptiveGateRiskInput()
    if not isinstance(compatibility, RegimeCompatibility):
        compatibility = RegimeCompatibility(str(compatibility).upper())
    key = promotion_key(gate_family_id, lane, market_state)
    age_seconds = (
        max(0.0, (now_ms - facts.last_outcome_at_ms) / 1000.0)
        if facts.last_outcome_at_ms is not None
        else None
    )
    fresh = age_seconds is not None and age_seconds <= active.evidence_max_age_seconds
    complete = facts.incomplete == 0 and facts.ambiguous == 0
    ev_pass = facts.ev_per_opportunity_usdc > active.min_ev_per_opportunity_usdc
    fast_pass = (
        fresh
        and complete
        and facts.evaluable >= active.fast_min_evaluable
        and facts.tp_first >= active.fast_min_tp_first
        and ev_pass
        and str(facts.last_outcome or "").lower() not in {"sl_first", "ambiguous_both"}
    )
    probe_non_loss = (
        facts.first_probe_net_pnl_usdc is not None
        and facts.first_probe_net_pnl_usdc >= 0.0
    )
    retry_after_failed_probe = bool(
        facts.first_probe_net_pnl_usdc is not None
        and facts.first_probe_net_pnl_usdc < 0.0
        and facts.first_probe_episode_id
        and facts.first_probe_episode_id != episode_id
    )
    effective_paid_complete = 0 if retry_after_failed_probe else facts.paid_complete
    effective_paid_wins = 0 if retry_after_failed_probe else facts.paid_wins
    effective_paid_net_pnl_usdc = (
        0.0 if retry_after_failed_probe else facts.paid_net_pnl_usdc
    )
    probation_shadow_pass = (
        facts.evaluable >= active.probation_min_evaluable
        and facts.tp_first >= active.probation_min_tp_first
        and ev_pass
    )
    probation_pass = fast_pass and probe_non_loss and (
        probation_shadow_pass or facts.matching_episode_count >= 2
    )
    control_pass = (
        probation_pass
        and facts.paid_integrity_complete
        and effective_paid_complete >= active.control_min_paid_complete
        and effective_paid_wins >= active.control_min_paid_wins
        and effective_paid_net_pnl_usdc > 0.0
        and ev_pass
    )
    evidence_gate = {
        "key": key,
        "fresh": fresh,
        "age_seconds": age_seconds,
        "last_outcome_at_ms": facts.last_outcome_at_ms,
        "data_complete": complete,
        "opportunities": facts.opportunities,
        "evaluable": facts.evaluable,
        "tp_first": facts.tp_first,
        "sl_first": facts.sl_first,
        "max_hold": facts.max_hold,
        "no_fill": facts.no_fill,
        "ev_per_opportunity_usdc": facts.ev_per_opportunity_usdc,
        "fast_pass": fast_pass,
        "probe_non_loss": probe_non_loss,
        "first_probe_episode_id": facts.first_probe_episode_id,
        "retry_after_failed_probe": retry_after_failed_probe,
        "probation_pass": probation_pass,
        "control_pass": control_pass,
        "matching_episode_count": facts.matching_episode_count,
        "paid_complete": effective_paid_complete,
        "paid_wins": effective_paid_wins,
        "paid_net_pnl_usdc": effective_paid_net_pnl_usdc,
    }

    common = dict(
        config=active,
        incumbent_accepted=bool(incumbent_accepted),
        gate_family_id=gate_family_id or "UNKNOWN",
        lane=lane or "UNKNOWN",
        market_state=market_state or "UNKNOWN",
        episode_id=episode_id or "UNKNOWN",
        evidence_gate=evidence_gate,
    )
    if not risk_facts.integrity_safe:
        return _decision(**common, action=AdaptiveActionMode.HALT, rule="v1461.integrity.halt", reason="integrity unsafe")
    if risk_facts.global_halted or risk_facts.cohort_net_pnl_usdc <= -active.cohort_net_loss_cap_usdc:
        return _decision(**common, action=AdaptiveActionMode.HALT, rule="v1461.risk.global_halt", reason="cohort paid path halted")
    if compatibility is RegimeCompatibility.HARD_BLOCK:
        return _decision(**common, action=AdaptiveActionMode.HARD_BLOCK, rule="v1461.regime.hard_block", reason="hard-block market state")
    if (
        risk_facts.key_quarantined
        or risk_facts.consecutive_paid_losses >= active.lane_loss_streak_limit
        or risk_facts.key_net_pnl_usdc <= -active.lane_net_loss_cap_usdc
    ):
        return _decision(**common, action=AdaptiveActionMode.SHADOW_BLOCK, rule="v1461.risk.quarantine", reason="route quarantined")
    if compatibility is RegimeCompatibility.ADVERSE:
        return _decision(**common, action=AdaptiveActionMode.SHADOW_BLOCK, rule="v1461.regime.adverse_block", reason="regime adverse to lane")

    if incumbent_accepted:
        if compatibility is RegimeCompatibility.SUPPORTIVE:
            return _decision(**common, action=AdaptiveActionMode.CONTROL, rule="v1461.incumbent.supportive_control", reason="supportive incumbent control")
        return _decision(**common, action=AdaptiveActionMode.PROBATION_0_5, rule="v1461.incumbent.uncertain_probation", reason="neutral or unknown incumbent capped")

    if not promotion_eligible:
        return _decision(**common, action=AdaptiveActionMode.SHADOW_BLOCK, rule="v1461.reject.not_promotable", reason="legacy gate not promotion eligible")
    if compatibility is not RegimeCompatibility.SUPPORTIVE:
        return _decision(**common, action=AdaptiveActionMode.SHADOW_BLOCK, rule="v1461.reject.requires_supportive", reason="rejected candidate requires supportive regime")
    if control_pass:
        return _decision(**common, action=AdaptiveActionMode.CONTROL, rule="v1461.promotion.control", reason="paid probation passed")
    if effective_paid_complete >= active.control_min_paid_complete:
        return _decision(**common, action=AdaptiveActionMode.SHADOW_BLOCK, rule="v1461.promotion.probation_failed", reason="paid probation did not pass control gate")
    if probation_pass:
        return _decision(**common, action=AdaptiveActionMode.PROBATION_0_5, rule="v1461.promotion.probation", reason="shadow and paid probe passed")
    token = promotion_token_id(active.policy_hash, gate_family_id, lane, market_state, episode_id)
    if fast_pass and not token_consumed:
        return _decision(**common, action=AdaptiveActionMode.FAST_PROBE_0_5, rule="v1461.promotion.fast_probe", reason="recent shadow evidence passed", token_id=token)
    return _decision(**common, action=AdaptiveActionMode.SHADOW_BLOCK, rule="v1461.reject.shadow_block", reason="promotion evidence insufficient or token consumed")


def _telemetry(decision: AdaptiveGateDecision, mode: OverlayMode) -> dict[str, Any]:
    return {
        "action_mode": decision.action_mode.value,
        "permits_order": decision.permits_order,
        "incumbent_accepted": decision.incumbent_accepted,
        "gate_family_id": decision.gate_family_id,
        "lane": decision.lane,
        "market_state": decision.market_state,
        "episode_id": decision.episode_id,
        "matrix_rule_id": decision.matrix_rule_id,
        "risk_scale": decision.risk_scale,
        "max_notional_usdc": decision.max_notional_usdc,
        "evidence_gate": dict(decision.evidence_gate),
        "token_id": decision.token_id,
        "policy_hash": decision.policy_hash,
        "reason": decision.reason,
        "mode": mode,
    }


def apply_adaptive_gate_decision(
    decision: CodexV1Decision,
    adaptive: AdaptiveGateDecision,
    *,
    mode: OverlayMode = "candidate-only",
) -> CodexV1Decision:
    """Apply admission/sizing only; exits, entry price and TTL remain untouched."""

    if mode not in ("candidate-only", "enforcement"):
        raise ValueError("mode must be candidate-only or enforcement")
    metrics = dict(decision.metrics) if isinstance(decision.metrics, Mapping) else {}
    metrics["v1461_adaptive_gate"] = _telemetry(adaptive, mode)
    if mode == "candidate-only":
        return replace(decision, metrics=metrics)
    if not adaptive.permits_order:
        return replace(
            decision,
            accepted=False,
            size_mult=0.0,
            notional_mult=0.0,
            requested_notional_usdc=0.0,
            metrics=metrics,
        )
    existing_cap = metrics.get("applied_notional_cap_usdc")
    try:
        cap = min(float(existing_cap), adaptive.max_notional_usdc)
        if not isfinite(cap) or cap <= 0.0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        cap = adaptive.max_notional_usdc
    metrics["applied_notional_cap_usdc"] = cap
    scale = adaptive.risk_scale
    return replace(
        decision,
        accepted=True,
        size_mult=max(0.0, decision.size_mult) * scale,
        notional_mult=max(0.0, decision.notional_mult) * scale,
        requested_notional_usdc=min(
            max(0.0, decision.requested_notional_usdc) * scale,
            adaptive.max_notional_usdc,
        ),
        metrics=metrics,
    )


__all__ = [
    "AdaptiveActionMode",
    "AdaptiveGateConfig",
    "AdaptiveGateDecision",
    "AdaptiveGateRiskInput",
    "GateEvidence",
    "RegimeCompatibility",
    "V1461_POLICY_NAME",
    "V1461_VERSION",
    "apply_adaptive_gate_decision",
    "promotion_key",
    "promotion_token_id",
    "select_adaptive_gate_decision",
]
