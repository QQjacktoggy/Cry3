"""Pure v1.4.60 lane/state adaptive policy.

The module is deliberately side-effect free.  It classifies an incumbent
decision into a closed lane/state action menu and can optionally apply only
admission and risk-size reductions to ``CodexV1Decision``.  It never changes
entry offsets, exits, TTL, or holding periods.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
from math import isfinite
from types import MappingProxyType
from typing import Any, Literal, Mapping

from src.gridbot.strategy.codex_v1_live import CodexV1Decision


V1460_VERSION = "v1.4.60B"
V1460_POLICY_NAME = "codex-v1.4.60B-lane-adaptive-risk-first"
OverlayMode = Literal["candidate-only", "enforcement"]


class AdaptiveActionMode(str, Enum):
    """Closed v1.4.60 action menu."""

    CONTROL = "CONTROL"
    PROBATION_0_5 = "PROBATION_0_5"
    SHADOW_BLOCK = "SHADOW_BLOCK"
    HALT = "HALT"


RP1_0BP_SHADOW = "rp1_entry_0bp_first_touch"
S1P_ONE_TICK_SHADOW = "s1p_entry_plus_minus_1_tick"
STUP_TIME_LOCK_SHADOW = "stup_clean_time_lock_4_5bp"
STUP_WEAK_OUTCOME_SHADOW = "stup_weak_state_first_touch_outcome"
CNL_BLOCKED_OUTCOME_SHADOW = "cnl_blocked_state_outcome"
GLOBAL_BLOCK_OUTCOME_SHADOW = "global_hard_block_state_outcome"

RULE_INTEGRITY_HALT = "v1460.integrity.halt"
RULE_GLOBAL_HALT = "v1460.risk.global_halt"
RULE_INCUMBENT_REJECTED = "v1460.incumbent.rejected"
RULE_LANE_ISOLATED = "v1460.risk.lane_state_isolated"
RULE_GLOBAL_STATE_BLOCK = "v1460.state.global_shadow_block"
RULE_UNKNOWN_PROBATION = "v1460.state.unknown_probation_0_5"
RULE_RP1_CONTROL = "v1460.rp1.control"
RULE_S1P_PULLBACK_CONTROL = "v1460.s1p_pullback.control"
RULE_STUP_CLEAN_CONTROL = "v1460.stup_clean.control"
RULE_STUP_WEAK_SHADOW = "v1460.stup_weak.shadow_block"
RULE_STUP_WEAK_PROBATION = "v1460.stup_weak.probation_0_5"
RULE_CNL_RECLAIM_CONTROL = "v1460.cnl_reclaim.control"
RULE_CNL_RISK_SHADOW = "v1460.cnl_risk.shadow_block"
RULE_OTHER_INCUMBENT_FALLBACK = "v1460.other.incumbent_fallback"


GLOBAL_SHADOW_BLOCK_STATES = frozenset(
    {
        "shock",
        "volatile",
        "volatility_shock",
        "stale_squeeze_top",
        "counter_recoil",
        "falling_discount_trap",
    }
)
S1P_PULLBACK_STATES = frozenset(
    {
        "ordinary_pullback",
        "pre_vwap",
        "ordinary_pullback_pre_vwap",
    }
)
STUP_CLEAN_STATES = frozenset(
    {
        "clean_extension",
        "hot_continuation",
        "strong_up_continuation",
    }
)
STUP_WEAK_STATES = frozenset(
    {
        "near_vwap_flat",
        "no_momentum_edge",
        "weak_chop",
        "mixed",
    }
)
CNL_CONTROL_STATES = frozenset(
    {
        "fast_reclaim",
        "discount_mixed",
        "discount_delayed_reclaim",
    }
)
CNL_SHADOW_BLOCK_STATES = frozenset(
    {
        "deep_discount_stable",
        "falling_discount_trap",
        "ambiguous",
    }
)
KNOWN_STATES = frozenset().union(
    GLOBAL_SHADOW_BLOCK_STATES,
    S1P_PULLBACK_STATES,
    STUP_CLEAN_STATES,
    STUP_WEAK_STATES,
    CNL_CONTROL_STATES,
    CNL_SHADOW_BLOCK_STATES,
)


def _finite_number(name: str, value: Any, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    converted = float(value)
    if not isfinite(converted):
        raise ValueError(f"{name} must be a finite number")
    if nonnegative and converted < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return converted


def _nonnegative_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _strict_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool")
    return value


@dataclass(frozen=True, slots=True)
class LaneAdaptiveConfig:
    """Immutable numeric contract for the closed v1.4.60 matrix."""

    version: str = V1460_VERSION
    policy_name: str = V1460_POLICY_NAME
    weak_min_evaluable: int = 8
    weak_min_tp_first: int = 6
    weak_min_cost_adjusted_ev_per_opportunity: float = 0.0
    weak_max_ambiguous: int = 0
    weak_max_incomplete: int = 0
    control_risk_scale: float = 1.0
    control_max_notional_usdc: float = 50.0
    probation_risk_scale: float = 0.5
    probation_max_notional_usdc: float = 25.0
    lane_loss_streak_limit: int = 2
    lane_loss_limit_usdc: float = 0.12
    cohort_loss_limit_usdc: float = 0.30

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("version must be a non-empty string")
        if not isinstance(self.policy_name, str) or not self.policy_name.strip():
            raise ValueError("policy_name must be a non-empty string")

        _nonnegative_int("weak_min_evaluable", self.weak_min_evaluable)
        _nonnegative_int("weak_min_tp_first", self.weak_min_tp_first)
        _nonnegative_int("weak_max_ambiguous", self.weak_max_ambiguous)
        _nonnegative_int("weak_max_incomplete", self.weak_max_incomplete)
        _nonnegative_int("lane_loss_streak_limit", self.lane_loss_streak_limit)

        # These lower bounds close the evidence gate against accidental loosening.
        if self.weak_min_evaluable < 8:
            raise ValueError("weak_min_evaluable must be at least 8")
        if self.weak_min_tp_first < 6:
            raise ValueError("weak_min_tp_first must be at least 6")
        if self.weak_min_tp_first > self.weak_min_evaluable:
            raise ValueError("weak_min_tp_first cannot exceed weak_min_evaluable")
        if self.weak_max_ambiguous != 0 or self.weak_max_incomplete != 0:
            raise ValueError("ambiguous and incomplete evidence thresholds are closed at zero")
        if not 1 <= self.lane_loss_streak_limit <= 2:
            raise ValueError("lane_loss_streak_limit must be in [1, 2]")

        weak_ev = _finite_number(
            "weak_min_cost_adjusted_ev_per_opportunity",
            self.weak_min_cost_adjusted_ev_per_opportunity,
            nonnegative=True,
        )
        control_scale = _finite_number(
            "control_risk_scale", self.control_risk_scale, nonnegative=True
        )
        probation_scale = _finite_number(
            "probation_risk_scale", self.probation_risk_scale, nonnegative=True
        )
        control_cap = _finite_number(
            "control_max_notional_usdc",
            self.control_max_notional_usdc,
            nonnegative=True,
        )
        probation_cap = _finite_number(
            "probation_max_notional_usdc",
            self.probation_max_notional_usdc,
            nonnegative=True,
        )
        lane_loss = _finite_number(
            "lane_loss_limit_usdc", self.lane_loss_limit_usdc, nonnegative=True
        )
        cohort_loss = _finite_number(
            "cohort_loss_limit_usdc", self.cohort_loss_limit_usdc, nonnegative=True
        )

        if weak_ev < 0.0:  # Kept explicit to document the closed non-negative gate.
            raise ValueError("weak EV threshold must be non-negative")
        if control_scale != 1.0:
            raise ValueError("CONTROL risk scale is closed at 1.0")
        if probation_scale != 0.5:
            raise ValueError("PROBATION_0_5 risk scale is closed at 0.5")
        if not 0.0 < control_cap <= 50.0:
            raise ValueError("control_max_notional_usdc must be in (0, 50]")
        if not 0.0 < probation_cap <= 25.0:
            raise ValueError("probation_max_notional_usdc must be in (0, 25]")
        if probation_cap > control_cap:
            raise ValueError("probation cap cannot exceed control cap")
        if not 0.0 < lane_loss <= 0.12:
            raise ValueError("lane_loss_limit_usdc must be in (0, 0.12]")
        if not 0.0 < cohort_loss <= 0.30:
            raise ValueError("cohort_loss_limit_usdc must be in (0, 0.30]")

    @property
    def policy_hash(self) -> str:
        return policy_hash(self)


@dataclass(frozen=True, slots=True)
class WeakStateShadowEvidence:
    """Cost-adjusted first-touch evidence for STUP weak states."""

    evaluable: int = 0
    tp_first: int = 0
    cost_adjusted_ev_per_opportunity: float = 0.0
    data_complete: bool = False
    ambiguous: int = 0
    incomplete: int = 0

    def __post_init__(self) -> None:
        _nonnegative_int("evaluable", self.evaluable)
        _nonnegative_int("tp_first", self.tp_first)
        _nonnegative_int("ambiguous", self.ambiguous)
        _nonnegative_int("incomplete", self.incomplete)
        _strict_bool("data_complete", self.data_complete)
        _finite_number(
            "cost_adjusted_ev_per_opportunity",
            self.cost_adjusted_ev_per_opportunity,
        )
        if self.tp_first > self.evaluable:
            raise ValueError("tp_first cannot exceed evaluable")


@dataclass(frozen=True, slots=True)
class LaneAdaptiveRiskInput:
    """Current reconciled risk facts; signed PnL values must only be finite."""

    integrity_safe: bool = True
    global_halted: bool = False
    lane_state_isolated: bool = False
    consecutive_complete_losses: int = 0
    lane_net_pnl_usdc: float = 0.0
    cohort_net_pnl_usdc: float = 0.0

    def __post_init__(self) -> None:
        _strict_bool("integrity_safe", self.integrity_safe)
        _strict_bool("global_halted", self.global_halted)
        _strict_bool("lane_state_isolated", self.lane_state_isolated)
        _nonnegative_int(
            "consecutive_complete_losses", self.consecutive_complete_losses
        )
        _finite_number("lane_net_pnl_usdc", self.lane_net_pnl_usdc)
        _finite_number("cohort_net_pnl_usdc", self.cohort_net_pnl_usdc)


@dataclass(frozen=True, slots=True)
class LaneAdaptiveDecision:
    """Immutable policy decision and complete audit telemetry."""

    action_mode: AdaptiveActionMode
    incumbent_accepted: bool
    matrix_rule_id: str
    risk_scale: float
    max_notional_usdc: float
    evidence_gate: Mapping[str, Any]
    policy_hash: str
    shadow_candidates: tuple[str, ...]
    reason: str
    permits_order: bool

    def __post_init__(self) -> None:
        if not isinstance(self.action_mode, AdaptiveActionMode):
            raise ValueError("action_mode must be AdaptiveActionMode")
        _strict_bool("incumbent_accepted", self.incumbent_accepted)
        _strict_bool("permits_order", self.permits_order)
        risk_scale = _finite_number("risk_scale", self.risk_scale, nonnegative=True)
        max_notional = _finite_number(
            "max_notional_usdc", self.max_notional_usdc, nonnegative=True
        )
        if risk_scale > 1.0:
            raise ValueError("risk_scale cannot increase incumbent risk")
        if not isinstance(self.matrix_rule_id, str) or not self.matrix_rule_id.strip():
            raise ValueError("matrix_rule_id must be non-empty")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be non-empty")
        if (
            not isinstance(self.policy_hash, str)
            or len(self.policy_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.policy_hash)
        ):
            raise ValueError("policy_hash must be a lowercase SHA-256 hex digest")
        if not isinstance(self.evidence_gate, Mapping):
            raise ValueError("evidence_gate must be a mapping")
        if not isinstance(self.shadow_candidates, tuple) or any(
            not isinstance(label, str) or not label.strip()
            for label in self.shadow_candidates
        ):
            raise ValueError("shadow_candidates must be a tuple of non-empty strings")

        expected_permit = (
            self.incumbent_accepted
            and self.action_mode
            in (AdaptiveActionMode.CONTROL, AdaptiveActionMode.PROBATION_0_5)
            and risk_scale > 0.0
            and max_notional > 0.0
        )
        if self.permits_order is not expected_permit:
            raise ValueError("permits_order is inconsistent with the action contract")
        if self.action_mode is AdaptiveActionMode.CONTROL and risk_scale != 1.0:
            raise ValueError("CONTROL decisions require risk_scale 1.0")
        if self.action_mode is AdaptiveActionMode.PROBATION_0_5:
            if risk_scale != 0.5 or max_notional > 25.0:
                raise ValueError("PROBATION_0_5 requires scale 0.5 and cap <= 25")
        if self.action_mode in (
            AdaptiveActionMode.SHADOW_BLOCK,
            AdaptiveActionMode.HALT,
        ) and (risk_scale != 0.0 or max_notional != 0.0):
            raise ValueError("blocked decisions must carry zero executable risk")

        object.__setattr__(self, "evidence_gate", MappingProxyType(dict(self.evidence_gate)))


_MATRIX_HASH_PAYLOAD = {
    "action_modes": [mode.value for mode in AdaptiveActionMode],
    "comparators": {
        "cohort_loss": "<= -threshold",
        "lane_loss": "<= -threshold",
        "lane_loss_streak": ">= threshold",
        "weak_ambiguous": "<= threshold",
        "weak_data_complete": "is true",
        "weak_evaluable": ">= threshold",
        "weak_ev": "> threshold",
        "weak_incomplete": "<= threshold",
        "weak_tp_first": ">= threshold",
    },
    "states": {
        "cnl_control": sorted(CNL_CONTROL_STATES),
        "cnl_shadow_block": sorted(CNL_SHADOW_BLOCK_STATES),
        "global_shadow_block": sorted(GLOBAL_SHADOW_BLOCK_STATES),
        "s1p_pullback": sorted(S1P_PULLBACK_STATES),
        "stup_clean": sorted(STUP_CLEAN_STATES),
        "stup_weak": sorted(STUP_WEAK_STATES),
    },
    "rules": {
        "cnl_control": RULE_CNL_RECLAIM_CONTROL,
        "cnl_shadow": RULE_CNL_RISK_SHADOW,
        "global_halt": RULE_GLOBAL_HALT,
        "global_state_block": RULE_GLOBAL_STATE_BLOCK,
        "incumbent_rejected": RULE_INCUMBENT_REJECTED,
        "integrity_halt": RULE_INTEGRITY_HALT,
        "lane_isolated": RULE_LANE_ISOLATED,
        "other_fallback": RULE_OTHER_INCUMBENT_FALLBACK,
        "rp1_control": RULE_RP1_CONTROL,
        "s1p_control": RULE_S1P_PULLBACK_CONTROL,
        "stup_clean": RULE_STUP_CLEAN_CONTROL,
        "stup_weak_probation": RULE_STUP_WEAK_PROBATION,
        "stup_weak_shadow": RULE_STUP_WEAK_SHADOW,
        "unknown_probation": RULE_UNKNOWN_PROBATION,
    },
    "shadow_labels": sorted(
        {
            CNL_BLOCKED_OUTCOME_SHADOW,
            GLOBAL_BLOCK_OUTCOME_SHADOW,
            RP1_0BP_SHADOW,
            S1P_ONE_TICK_SHADOW,
            STUP_TIME_LOCK_SHADOW,
            STUP_WEAK_OUTCOME_SHADOW,
        }
    ),
}


def policy_hash(config: LaneAdaptiveConfig | None = None) -> str:
    """Return a deterministic identity for all thresholds and matrix content."""

    active = config or LaneAdaptiveConfig()
    if not isinstance(active, LaneAdaptiveConfig):
        raise TypeError("config must be LaneAdaptiveConfig")
    canonical = {
        "config": {
            "cohort_loss_limit_usdc": active.cohort_loss_limit_usdc,
            "control_max_notional_usdc": active.control_max_notional_usdc,
            "control_risk_scale": active.control_risk_scale,
            "lane_loss_limit_usdc": active.lane_loss_limit_usdc,
            "lane_loss_streak_limit": active.lane_loss_streak_limit,
            "policy_name": active.policy_name,
            "probation_max_notional_usdc": active.probation_max_notional_usdc,
            "probation_risk_scale": active.probation_risk_scale,
            "version": active.version,
            "weak_max_ambiguous": active.weak_max_ambiguous,
            "weak_max_incomplete": active.weak_max_incomplete,
            "weak_min_cost_adjusted_ev_per_opportunity": (
                active.weak_min_cost_adjusted_ev_per_opportunity
            ),
            "weak_min_evaluable": active.weak_min_evaluable,
            "weak_min_tp_first": active.weak_min_tp_first,
        },
        "matrix": _MATRIX_HASH_PAYLOAD,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_lane(lane: str | None) -> str | None:
    if lane is None:
        return None
    if not isinstance(lane, str):
        raise TypeError("lane must be str or None")
    normalized = lane.strip().upper().replace("_", "-")
    return normalized or None


def _normalize_state(market_state: str | None) -> str | None:
    if market_state is None:
        return None
    if not isinstance(market_state, str):
        raise TypeError("market_state must be str or None")
    normalized = market_state.strip().lower()
    if not normalized:
        return None
    normalized = normalized.rsplit(":", 1)[-1]
    return normalized.replace("-", "_").replace(" ", "_")


def _decision(
    *,
    config: LaneAdaptiveConfig,
    incumbent_accepted: bool,
    action_mode: AdaptiveActionMode,
    matrix_rule_id: str,
    reason: str,
    shadow_candidates: tuple[str, ...] = (),
    evidence_gate: Mapping[str, Any] | None = None,
) -> LaneAdaptiveDecision:
    if action_mode is AdaptiveActionMode.CONTROL:
        risk_scale = config.control_risk_scale
        max_notional = config.control_max_notional_usdc
    elif action_mode is AdaptiveActionMode.PROBATION_0_5:
        risk_scale = config.probation_risk_scale
        max_notional = min(config.probation_max_notional_usdc, 25.0)
    else:
        risk_scale = 0.0
        max_notional = 0.0
    permits_order = (
        incumbent_accepted
        and action_mode
        in (AdaptiveActionMode.CONTROL, AdaptiveActionMode.PROBATION_0_5)
    )
    return LaneAdaptiveDecision(
        action_mode=action_mode,
        incumbent_accepted=incumbent_accepted,
        matrix_rule_id=matrix_rule_id,
        risk_scale=risk_scale,
        max_notional_usdc=max_notional,
        evidence_gate=evidence_gate or {"required": False},
        policy_hash=config.policy_hash,
        shadow_candidates=shadow_candidates,
        reason=reason,
        permits_order=permits_order,
    )


def _weak_evidence_gate(
    evidence: WeakStateShadowEvidence,
    config: LaneAdaptiveConfig,
) -> dict[str, Any]:
    evaluable_pass = evidence.evaluable >= config.weak_min_evaluable
    tp_first_pass = evidence.tp_first >= config.weak_min_tp_first
    ev_pass = (
        evidence.cost_adjusted_ev_per_opportunity
        > config.weak_min_cost_adjusted_ev_per_opportunity
    )
    ambiguous_pass = evidence.ambiguous <= config.weak_max_ambiguous
    incomplete_pass = evidence.incomplete <= config.weak_max_incomplete
    qualified = (
        evaluable_pass
        and tp_first_pass
        and ev_pass
        and evidence.data_complete
        and ambiguous_pass
        and incomplete_pass
    )
    return {
        "required": True,
        "evaluable": evidence.evaluable,
        "min_evaluable": config.weak_min_evaluable,
        "evaluable_pass": evaluable_pass,
        "tp_first": evidence.tp_first,
        "min_tp_first": config.weak_min_tp_first,
        "tp_first_pass": tp_first_pass,
        "cost_adjusted_ev_per_opportunity": (
            evidence.cost_adjusted_ev_per_opportunity
        ),
        "min_cost_adjusted_ev_per_opportunity_exclusive": (
            config.weak_min_cost_adjusted_ev_per_opportunity
        ),
        "ev_pass": ev_pass,
        "data_complete": evidence.data_complete,
        "ambiguous": evidence.ambiguous,
        "max_ambiguous": config.weak_max_ambiguous,
        "ambiguous_pass": ambiguous_pass,
        "incomplete": evidence.incomplete,
        "max_incomplete": config.weak_max_incomplete,
        "incomplete_pass": incomplete_pass,
        "qualified": qualified,
    }


def select_lane_adaptive_decision(
    *,
    lane: str | None,
    market_state: str | None,
    incumbent_accepted: bool,
    weak_evidence: WeakStateShadowEvidence | None = None,
    risk: LaneAdaptiveRiskInput | None = None,
    config: LaneAdaptiveConfig | None = None,
) -> LaneAdaptiveDecision:
    """Select one deterministic lane/state action without admitting new orders."""

    _strict_bool("incumbent_accepted", incumbent_accepted)
    active_config = config or LaneAdaptiveConfig()
    active_risk = risk or LaneAdaptiveRiskInput()
    active_evidence = weak_evidence or WeakStateShadowEvidence()
    if not isinstance(active_config, LaneAdaptiveConfig):
        raise TypeError("config must be LaneAdaptiveConfig")
    if not isinstance(active_risk, LaneAdaptiveRiskInput):
        raise TypeError("risk must be LaneAdaptiveRiskInput")
    if not isinstance(active_evidence, WeakStateShadowEvidence):
        raise TypeError("weak_evidence must be WeakStateShadowEvidence")

    normalized_lane = _normalize_lane(lane)
    normalized_state = _normalize_state(market_state)

    if not active_risk.integrity_safe:
        return _decision(
            config=active_config,
            incumbent_accepted=incumbent_accepted,
            action_mode=AdaptiveActionMode.HALT,
            matrix_rule_id=RULE_INTEGRITY_HALT,
            reason="integrity is unsafe; halt paid execution",
        )
    if (
        active_risk.global_halted
        or active_risk.cohort_net_pnl_usdc
        <= -active_config.cohort_loss_limit_usdc
    ):
        return _decision(
            config=active_config,
            incumbent_accepted=incumbent_accepted,
            action_mode=AdaptiveActionMode.HALT,
            matrix_rule_id=RULE_GLOBAL_HALT,
            reason="global halt flag or closed cohort loss limit reached",
        )
    if not incumbent_accepted:
        return _decision(
            config=active_config,
            incumbent_accepted=False,
            action_mode=AdaptiveActionMode.SHADOW_BLOCK,
            matrix_rule_id=RULE_INCUMBENT_REJECTED,
            reason="incumbent rejected; adaptive policy cannot create admission",
        )
    if (
        active_risk.lane_state_isolated
        or active_risk.consecutive_complete_losses
        >= active_config.lane_loss_streak_limit
        or active_risk.lane_net_pnl_usdc <= -active_config.lane_loss_limit_usdc
    ):
        return _decision(
            config=active_config,
            incumbent_accepted=True,
            action_mode=AdaptiveActionMode.SHADOW_BLOCK,
            matrix_rule_id=RULE_LANE_ISOLATED,
            reason="lane/state isolation or closed lane loss limit reached",
        )
    cnl_specific_trap = bool(
        normalized_lane == "CNL-WPR-L"
        and normalized_state == "falling_discount_trap"
    )
    if normalized_state in GLOBAL_SHADOW_BLOCK_STATES and not cnl_specific_trap:
        return _decision(
            config=active_config,
            incumbent_accepted=True,
            action_mode=AdaptiveActionMode.SHADOW_BLOCK,
            matrix_rule_id=RULE_GLOBAL_STATE_BLOCK,
            reason="global risk state is hard-blocked into shadow",
            shadow_candidates=(GLOBAL_BLOCK_OUTCOME_SHADOW,),
        )
    # RP1 remains the incumbent control for every non-hard-blocked regime.
    # Real live evidence uses labels such as RP1:unchanged and RP1:mixed;
    # requiring those states to appear in another lane's finite state set
    # would silently drop the required 0bp first-touch shadow annotation.
    if normalized_lane == "RP1":
        return _decision(
            config=active_config,
            incumbent_accepted=True,
            action_mode=AdaptiveActionMode.CONTROL,
            matrix_rule_id=RULE_RP1_CONTROL,
            reason="accepted RP1 remains incumbent control",
            shadow_candidates=(RP1_0BP_SHADOW,),
        )
    if normalized_state is None or normalized_state not in KNOWN_STATES:
        return _decision(
            config=active_config,
            incumbent_accepted=True,
            action_mode=AdaptiveActionMode.PROBATION_0_5,
            matrix_rule_id=RULE_UNKNOWN_PROBATION,
            reason=(
                "unknown or missing state; retain incumbent admission at "
                "half-risk probation"
            ),
        )

    if normalized_lane == "S1P-L" and normalized_state in S1P_PULLBACK_STATES:
        return _decision(
            config=active_config,
            incumbent_accepted=True,
            action_mode=AdaptiveActionMode.CONTROL,
            matrix_rule_id=RULE_S1P_PULLBACK_CONTROL,
            reason="accepted S1P-L pullback remains incumbent control",
            shadow_candidates=(S1P_ONE_TICK_SHADOW,),
        )
    if normalized_lane == "STUP-S" and normalized_state in STUP_CLEAN_STATES:
        return _decision(
            config=active_config,
            incumbent_accepted=True,
            action_mode=AdaptiveActionMode.CONTROL,
            matrix_rule_id=RULE_STUP_CLEAN_CONTROL,
            reason="accepted STUP-S clean continuation remains incumbent control",
            shadow_candidates=(STUP_TIME_LOCK_SHADOW,),
        )
    if normalized_lane == "STUP-S" and normalized_state in STUP_WEAK_STATES:
        gate = _weak_evidence_gate(active_evidence, active_config)
        if gate["qualified"]:
            return _decision(
                config=active_config,
                incumbent_accepted=True,
                action_mode=AdaptiveActionMode.PROBATION_0_5,
                matrix_rule_id=RULE_STUP_WEAK_PROBATION,
                reason="STUP-S weak-state shadow gate passed; half-risk probation only",
                shadow_candidates=(STUP_WEAK_OUTCOME_SHADOW,),
                evidence_gate=gate,
            )
        return _decision(
            config=active_config,
            incumbent_accepted=True,
            action_mode=AdaptiveActionMode.SHADOW_BLOCK,
            matrix_rule_id=RULE_STUP_WEAK_SHADOW,
            reason="STUP-S weak-state evidence gate has not passed",
            shadow_candidates=(STUP_WEAK_OUTCOME_SHADOW,),
            evidence_gate=gate,
        )
    if normalized_lane == "CNL-WPR-L" and normalized_state in CNL_CONTROL_STATES:
        return _decision(
            config=active_config,
            incumbent_accepted=True,
            action_mode=AdaptiveActionMode.CONTROL,
            matrix_rule_id=RULE_CNL_RECLAIM_CONTROL,
            reason="accepted CNL-WPR-L reclaim remains incumbent control",
        )
    if normalized_lane == "CNL-WPR-L" and normalized_state in CNL_SHADOW_BLOCK_STATES:
        return _decision(
            config=active_config,
            incumbent_accepted=True,
            action_mode=AdaptiveActionMode.SHADOW_BLOCK,
            matrix_rule_id=RULE_CNL_RISK_SHADOW,
            reason="CNL-WPR-L deep, trap, or ambiguous state remains shadow-only",
            shadow_candidates=(CNL_BLOCKED_OUTCOME_SHADOW,),
        )

    return _decision(
        config=active_config,
        incumbent_accepted=True,
        action_mode=AdaptiveActionMode.CONTROL,
        matrix_rule_id=RULE_OTHER_INCUMBENT_FALLBACK,
        reason="accepted lane/state is outside the finite overlay; preserve incumbent",
    )


def _telemetry_payload(
    adaptive: LaneAdaptiveDecision,
    mode: OverlayMode,
) -> dict[str, Any]:
    return {
        "action_mode": adaptive.action_mode.value,
        "incumbent_accepted": adaptive.incumbent_accepted,
        "matrix_rule_id": adaptive.matrix_rule_id,
        "risk_scale": adaptive.risk_scale,
        "max_notional_usdc": adaptive.max_notional_usdc,
        "evidence_gate": dict(adaptive.evidence_gate),
        "policy_hash": adaptive.policy_hash,
        "shadow_candidates": adaptive.shadow_candidates,
        "reason": adaptive.reason,
        "permits_order": adaptive.permits_order,
        "mode": mode,
    }


def apply_lane_adaptive_decision(
    decision: CodexV1Decision,
    adaptive: LaneAdaptiveDecision,
    *,
    mode: OverlayMode = "candidate-only",
) -> CodexV1Decision:
    """Annotate, block, or reduce sizing without changing any strategy parameter."""

    if not isinstance(decision, CodexV1Decision):
        raise TypeError("decision must be CodexV1Decision")
    if not isinstance(adaptive, LaneAdaptiveDecision):
        raise TypeError("adaptive must be LaneAdaptiveDecision")
    if mode not in ("candidate-only", "enforcement"):
        raise ValueError("mode must be candidate-only or enforcement")

    metrics = dict(decision.metrics) if isinstance(decision.metrics, Mapping) else {}
    metrics["v1460_lane_adaptive"] = _telemetry_payload(adaptive, mode)
    if mode == "candidate-only":
        return replace(decision, metrics=metrics)

    for name, value in (
        ("size_mult", decision.size_mult),
        ("notional_mult", decision.notional_mult),
        ("requested_notional_usdc", decision.requested_notional_usdc),
    ):
        _finite_number(name, value, nonnegative=True)

    if not decision.accepted or not adaptive.permits_order:
        return replace(
            decision,
            accepted=False,
            size_mult=0.0,
            notional_mult=0.0,
            requested_notional_usdc=0.0,
            metrics=metrics,
        )

    risk_scale = min(1.0, adaptive.risk_scale)
    existing_cap = metrics.get("applied_notional_cap_usdc")
    try:
        existing_cap_value = float(existing_cap)
    except (TypeError, ValueError, OverflowError):
        existing_cap_value = None
    if (
        existing_cap_value is not None
        and isfinite(existing_cap_value)
        and existing_cap_value > 0.0
    ):
        metrics["applied_notional_cap_usdc"] = min(
            existing_cap_value,
            adaptive.max_notional_usdc,
        )
    else:
        metrics["applied_notional_cap_usdc"] = adaptive.max_notional_usdc
    return replace(
        decision,
        accepted=True,
        size_mult=min(decision.size_mult, decision.size_mult * risk_scale),
        notional_mult=min(
            decision.notional_mult,
            decision.notional_mult * risk_scale,
        ),
        requested_notional_usdc=min(
            decision.requested_notional_usdc,
            decision.requested_notional_usdc * risk_scale,
            adaptive.max_notional_usdc,
        ),
        metrics=metrics,
    )


# Explicit aliases keep the public surface easy to discover without adding a runtime.
evaluate_lane_adaptive = select_lane_adaptive_decision
apply_v1460_lane_adaptive = apply_lane_adaptive_decision
V1460LaneAdaptiveConfig = LaneAdaptiveConfig
V1460LaneAdaptiveDecision = LaneAdaptiveDecision


__all__ = [
    "AdaptiveActionMode",
    "CNL_CONTROL_STATES",
    "CNL_SHADOW_BLOCK_STATES",
    "GLOBAL_SHADOW_BLOCK_STATES",
    "LaneAdaptiveConfig",
    "LaneAdaptiveDecision",
    "LaneAdaptiveRiskInput",
    "OverlayMode",
    "S1P_PULLBACK_STATES",
    "STUP_CLEAN_STATES",
    "STUP_WEAK_STATES",
    "V1460LaneAdaptiveConfig",
    "V1460LaneAdaptiveDecision",
    "WeakStateShadowEvidence",
    "apply_lane_adaptive_decision",
    "apply_v1460_lane_adaptive",
    "evaluate_lane_adaptive",
    "policy_hash",
    "select_lane_adaptive_decision",
]
