"""Exact paid execution plan derived from one v1.4.69 adaptive authority."""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
from typing import Any, Mapping

from src.gridbot.mainnet.v1469_adaptive_identity import TakeProfitLevel
from src.gridbot.mainnet.v1469_arm_arbiter import LeasePhase
from src.gridbot.mainnet.v1469_arm_profiles import get_arm_profile
from src.gridbot.mainnet.v1469_authority_runtime import AuthorityRuntimeResult
from src.gridbot.strategy.wildcat_live import WildcatLiveDecision


@dataclass(frozen=True, slots=True)
class V1469PaidExecutionPlan:
    arm_key: str
    lease_id: str
    lease_generation: int
    evidence_revision: str
    lane_code: str
    side: str
    strategy: str
    regime: str
    execution_profile_id: str
    execution_profile_hash: str
    risk_policy_hash: str
    notional_cap_usdc: float
    entry_offset_bp: float
    entry_ttl_s: int
    maker_mode: str
    take_profits: tuple[TakeProfitLevel, ...]
    sl_bp: float
    max_hold_s: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "v1469.paid-execution-plan.1",
            "arm_key": self.arm_key,
            "lease_id": self.lease_id,
            "lease_generation": self.lease_generation,
            "evidence_revision": self.evidence_revision,
            "lane_code": self.lane_code,
            "side": self.side,
            "strategy": self.strategy,
            "regime": self.regime,
            "execution_profile_id": self.execution_profile_id,
            "execution_profile_hash": self.execution_profile_hash,
            "risk_policy_hash": self.risk_policy_hash,
            "notional_cap_usdc": self.notional_cap_usdc,
            "entry_offset_bp": self.entry_offset_bp,
            "entry_ttl_s": self.entry_ttl_s,
            "maker_mode": self.maker_mode,
            "take_profits": [
                item.to_payload() for item in self.take_profits
            ],
            "sl_bp": self.sl_bp,
            "max_hold_s": self.max_hold_s,
        }


def build_paid_execution_plan(
    authority: AuthorityRuntimeResult,
    *,
    approved_notional_usdc: float,
) -> V1469PaidExecutionPlan:
    """Freeze an executable closed-menu plan or fail before any order API."""

    if not isinstance(authority, AuthorityRuntimeResult):
        raise TypeError("authority must be AuthorityRuntimeResult")
    if not authority.submit_admissible:
        raise ValueError("authority is not submit-admissible")
    winner = authority.winner
    current = authority.current_opportunity
    lease = authority.durable_lease
    if winner is None or current is None or lease is None:
        raise ValueError("authority is missing winner/current opportunity/lease")
    if (
        winner.arm_key != current.arm_key
        or winner.arm_key != lease.arm_key
        or winner.execution_profile_hash != current.execution_profile_hash
        or winner.execution_profile_hash != lease.execution_profile_hash
        or authority.decision.evidence_revision != lease.evidence_revision
    ):
        raise ValueError("authority identity changed before plan freeze")
    profile = get_arm_profile(winner.execution_profile_id)
    execution = profile.execution_profile
    if execution is None or profile.risk_off:
        raise ValueError("winner has no paid execution geometry")
    if execution.profile_hash != winner.execution_profile_hash:
        raise ValueError("closed-menu profile hash mismatch")

    # Phase-C entry support is intentionally narrower than the general schema.
    # Unsupported dynamic controls remain shadow-only until each receives its
    # own exchange-path acceptance tests.
    if execution.maker_mode != "POST_ONLY":
        raise ValueError("paid execution requires POST_ONLY")
    if (
        execution.reprice.enabled
        or execution.breakeven.enabled
        or execution.trail.enabled
        or execution.runner.enabled
        or execution.early_fail.enabled
        or execution.dca.enabled
    ):
        raise ValueError("dynamic execution controls are not paid-ready")

    try:
        approved = float(approved_notional_usdc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("approved_notional_usdc must be finite and positive") from exc
    if not isfinite(approved) or approved <= 0:
        raise ValueError("approved_notional_usdc must be finite and positive")
    if approved > float(lease.notional_cap_usdc) + 1e-12:
        raise ValueError("approved notional exceeds durable lease cap")
    if lease.phase not in {LeasePhase.PROBATION, LeasePhase.LIVE}:
        raise ValueError("durable lease phase is not paid-capable")
    phase_cap = 25.0 if lease.phase is LeasePhase.PROBATION else 50.0
    if approved > phase_cap + 1e-12:
        raise ValueError("approved notional exceeds lease phase cap")

    return V1469PaidExecutionPlan(
        arm_key=winner.arm_key,
        lease_id=lease.lease_id,
        lease_generation=int(lease.generation),
        evidence_revision=lease.evidence_revision,
        lane_code=winner.lane_code,
        side=winner.side,
        strategy=winner.strategy,
        regime=winner.regime,
        execution_profile_id=winner.execution_profile_id,
        execution_profile_hash=winner.execution_profile_hash,
        risk_policy_hash=lease.risk_policy_hash,
        notional_cap_usdc=approved,
        entry_offset_bp=float(execution.entry_offset_bp),
        entry_ttl_s=int(execution.entry_ttl_s),
        maker_mode=execution.maker_mode,
        take_profits=execution.take_profits,
        sl_bp=float(execution.sl_bp),
        max_hold_s=int(execution.max_hold_s),
    )


def apply_paid_execution_plan(
    decision: WildcatLiveDecision,
    plan: V1469PaidExecutionPlan,
    *,
    reference_price: float,
    leverage: int,
) -> WildcatLiveDecision:
    """Apply exact entry/TP/SL/hold sizing without global TP overlays."""

    if not isinstance(decision, WildcatLiveDecision):
        raise TypeError("decision must be WildcatLiveDecision")
    if not isinstance(plan, V1469PaidExecutionPlan):
        raise TypeError("plan must be V1469PaidExecutionPlan")
    if str(decision.side or "").strip().upper() != plan.side:
        raise ValueError("decision side does not match paid plan")
    try:
        reference = float(reference_price)
        leverage_value = int(leverage)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("reference price/leverage is invalid") from exc
    if not isfinite(reference) or reference <= 0 or leverage_value <= 0:
        raise ValueError("reference price/leverage is invalid")

    direction = 1.0 if plan.side == "LONG" else -1.0
    entry = reference * (1.0 - direction * plan.entry_offset_bp / 10_000.0)
    stop = entry * (1.0 - direction * plan.sl_bp / 10_000.0)
    targets = [
        entry * (1.0 + direction * level.target_bp / 10_000.0)
        for level in plan.take_profits
    ]
    signal = replace(
        decision.signal,
        entries=[entry],
        stop_loss=stop,
        take_profits=targets,
        planned_notional_usdc=plan.notional_cap_usdc,
        planned_margin_usdc=plan.notional_cap_usdc / leverage_value,
        planned_qty=plan.notional_cap_usdc / entry,
        risk_amount_usdc=(
            plan.notional_cap_usdc * plan.sl_bp / 10_000.0
        ),
    )
    first = plan.take_profits[0]
    last = plan.take_profits[-1]
    return replace(
        decision,
        signal=signal,
        strategy=plan.strategy,
        side=plan.side,
        tp_pct=last.target_bp / 10_000.0,
        sl_pct=plan.sl_bp / 10_000.0,
        partial_exit_pct=(
            first.fraction if len(plan.take_profits) > 1 else 1.0
        ),
        partial_tp_pct=first.target_bp / 10_000.0,
        recovery_steps=0,
        adverse_exit_bars=0,
        adverse_exit_loss_pct=0.0,
        max_holding_bars=max(1, plan.max_hold_s // 60),
        params_label=f"v1469:{plan.execution_profile_id}",
    )


def execution_plan_from_signal(
    signal: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return the persisted exact plan payload for downstream exit routing."""

    value = signal.get("v1469_paid_execution")
    return value if isinstance(value, Mapping) else None


__all__ = [
    "V1469PaidExecutionPlan",
    "apply_paid_execution_plan",
    "build_paid_execution_plan",
    "execution_plan_from_signal",
]
