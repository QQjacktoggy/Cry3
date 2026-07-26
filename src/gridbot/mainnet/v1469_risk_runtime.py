"""Durable submit-time risk admission for v1.4.69 paid entries."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from src.gridbot.mainnet.v1469_adaptive_identity import RiskPolicy
from src.gridbot.mainnet.v1469_arm_arbiter import LeasePhase
from src.gridbot.mainnet.v1469_authority_runtime import AuthorityRuntimeResult
from src.gridbot.mainnet.v1469_risk_policy import (
    DailyRiskSnapshot,
    NotionalCapDecision,
    NotionalCapRequest,
    RiskStage,
    evaluate_notional_cap,
    reduce_daily_risk,
)
from src.gridbot.storage.v1469_risk_event_repository import (
    V1469RiskEventRepository,
)


@dataclass(frozen=True, slots=True)
class V1469RiskAdmission:
    allowed: bool
    reason: str
    approved_notional_usdc: float
    reserved_loss_usdc: float
    snapshot: DailyRiskSnapshot
    cap_decision: NotionalCapDecision


class V1469RiskAdmissionRuntime:
    """Rebuild the active TPE day and size one exact authority."""

    def __init__(self, repository: V1469RiskEventRepository) -> None:
        self._repository = repository

    async def evaluate(
        self,
        authority: AuthorityRuntimeResult,
        *,
        desired_notional_usdc: float,
        sl_bp: float,
        roundtrip_fee_bp: float,
        slippage_bp: float,
        exchange_min_notional_usdc: float,
        policy: RiskPolicy,
        now_ms: int,
        ledger_limit: int = 10_000,
    ) -> V1469RiskAdmission:
        if not isinstance(authority, AuthorityRuntimeResult):
            raise TypeError("authority must be AuthorityRuntimeResult")
        lease = authority.durable_lease
        if not authority.submit_admissible or lease is None:
            raise ValueError("authority is not submit-admissible")
        if not isinstance(policy, RiskPolicy):
            raise TypeError("policy must be RiskPolicy")
        try:
            desired = float(desired_notional_usdc)
            minimum = float(exchange_min_notional_usdc)
            now = int(now_ms)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("risk admission inputs are invalid") from exc
        if (
            not isfinite(desired)
            or desired <= 0
            or not isfinite(minimum)
            or minimum < 0
            or now < 0
        ):
            raise ValueError("risk admission inputs are invalid")
        if lease.risk_policy_hash != policy.policy_hash:
            raise ValueError("lease risk-policy hash mismatch")

        events = await self._repository.load_active_day_events(
            environment=lease.environment,
            symbol=lease.symbol,
            as_of_ms=now,
            limit=int(ledger_limit),
        )
        snapshot = reduce_daily_risk(
            events,
            as_of_ms=now,
            policy=policy,
            expected_risk_policy_hash=lease.risk_policy_hash,
        )
        stage = (
            RiskStage.PROBATION
            if lease.phase is LeasePhase.PROBATION
            else RiskStage.LIVE
            if lease.phase is LeasePhase.LIVE
            else RiskStage.SHADOW
        )
        cap = evaluate_notional_cap(
            NotionalCapRequest(
                stage=stage,
                global_cap_usdc=policy.global_open_notional_cap_usdc,
                lane_cap_usdc=policy.lane_open_notional_cap_usdc,
                remaining_daily_risk_usdc=snapshot.remaining_daily_risk_usdc,
                sl_bp=float(sl_bp),
                roundtrip_fee_bp=float(roundtrip_fee_bp),
                slippage_bp=float(slippage_bp),
                exchange_min_notional_usdc=minimum,
                now_ms=now,
                expected_risk_policy_hash=lease.risk_policy_hash,
            ),
            snapshot,
            policy=policy,
        )
        approved = min(
            desired,
            float(lease.notional_cap_usdc),
            float(cap.notional_cap_usdc),
        )
        if not cap.allowed:
            approved = 0.0
            reason = cap.reason
        elif approved < minimum:
            approved = 0.0
            reason = "desired_notional_below_exchange_minimum"
        else:
            reason = cap.reason
        reserved_loss = (
            approved * float(cap.all_in_loss_bp) / 10_000.0
            if approved > 0
            else 0.0
        )
        if reserved_loss > snapshot.remaining_daily_risk_usdc + 1e-12:
            approved = 0.0
            reserved_loss = 0.0
            reason = "reservation_exceeds_daily_risk"
        return V1469RiskAdmission(
            allowed=approved > 0,
            reason=reason,
            approved_notional_usdc=approved,
            reserved_loss_usdc=reserved_loss,
            snapshot=snapshot,
            cap_decision=cap,
        )

def risk_policy_from_settings(settings: object) -> RiskPolicy:
    """Build the one canonical policy shared by lease, claim, and daily risk."""

    return RiskPolicy(
        policy_id="V1469_PHASE_C",
        paid_notional_cap_usdc=float(
            getattr(settings, "mainnet_codex_v1469_live_notional_usdc")
        ),
        per_trade_loss_cap_usdc=float(
            getattr(settings, "mainnet_codex_v1469_per_trade_loss_cap_usdc")
        ),
        lane_open_notional_cap_usdc=float(
            getattr(settings, "mainnet_codex_v1469_lane_open_notional_usdc")
        ),
        global_open_notional_cap_usdc=float(
            getattr(settings, "mainnet_codex_v1469_global_open_notional_usdc")
        ),
        daily_soft_loss_cap_usdc=float(
            getattr(settings, "mainnet_codex_v1469_daily_soft_loss_usdc")
        ),
        daily_hard_loss_cap_usdc=float(
            getattr(settings, "mainnet_codex_v1469_daily_hard_loss_usdc")
        ),
        daily_profit_lock_trigger_usdc=float(
            getattr(
                settings,
                "mainnet_codex_v1469_daily_profit_lock_trigger_usdc",
            )
        ),
        daily_profit_lock_giveback_usdc=float(
            getattr(
                settings,
                "mainnet_codex_v1469_daily_profit_lock_giveback_usdc",
            )
        ),
        max_consecutive_losses=2,
        cooldown_s=300,
    )


__all__ = [
    "V1469RiskAdmission",
    "V1469RiskAdmissionRuntime",
    "risk_policy_from_settings",
]