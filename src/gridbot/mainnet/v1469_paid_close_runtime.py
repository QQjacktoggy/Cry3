"""Conservative paid-close accounting for v1.4.69 claims."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Mapping

from src.gridbot.mainnet.v1469_risk_policy import DailyRiskEvent
from src.gridbot.storage.v1469_paid_execution_claim_repository import (
    DurablePaidExecutionClaim,
    PaidClaimMutationResult,
    V1469PaidClaimConflictError,
    V1469PaidExecutionClaimRepository,
)
from src.gridbot.storage.v1469_risk_event_repository import (
    V1469RiskEventRepository,
)


@dataclass(frozen=True, slots=True)
class PaidCloseResult:
    claim: DurablePaidExecutionClaim
    risk_event_inserted: bool
    terminal_mutation: PaidClaimMutationResult | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class PaidNoFillResult:
    claim: DurablePaidExecutionClaim
    terminal_mutation: PaidClaimMutationResult | None
    replayed: bool


class V1469PaidCloseRuntime:
    """Write PnL before releasing a claim's outstanding risk reservation.

    The ordering is intentionally conservative.  A crash after the daily-risk
    event but before terminalizing the claim leaves the reservation active and
    therefore blocks too much risk; it can never release risk without first
    recording the paid PnL.  Retrying with the same close facts is idempotent.
    """

    def __init__(
        self,
        claim_repository: V1469PaidExecutionClaimRepository,
        risk_repository: V1469RiskEventRepository,
    ) -> None:
        self._claim_repository = claim_repository
        self._risk_repository = risk_repository

    async def record_close(
        self,
        *,
        claim_id: str,
        fee_net_pnl_usdc: float,
        terminal_reason: str,
        occurred_at_ms: int,
        source_run_id: str,
        hard_loss_marker: bool = False,
        actor: str = "v1469-paid-close-runtime",
    ) -> PaidCloseResult:
        claim_key = str(claim_id or "").strip()
        reason = str(terminal_reason or "").strip()
        run_id = str(source_run_id or "").strip()
        event_actor = str(actor or "").strip()
        try:
            pnl = float(fee_net_pnl_usdc)
            at_ms = int(occurred_at_ms)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("paid close inputs are invalid") from exc
        if (
            not claim_key
            or not reason
            or not run_id
            or not event_actor
            or not isfinite(pnl)
            or at_ms < 0
        ):
            raise ValueError("paid close inputs are invalid")

        claim = await self._claim_repository.get_claim_by_id(claim_key)
        if claim is None:
            raise V1469PaidClaimConflictError("paid claim is missing")
        if claim.status not in {"SUBMITTED", "TERMINAL"}:
            raise V1469PaidClaimConflictError(
                "paid close requires SUBMITTED or replayed TERMINAL claim"
            )
        event_id = f"v1469-paid-close:{claim.claim_id}"
        hard_loss = bool(hard_loss_marker)
        if claim.status == "TERMINAL":
            result = claim.result_payload
            try:
                durable_pnl = (
                    float(result.get("fee_net_pnl_usdc"))
                    if isinstance(result, Mapping)
                    else None
                )
            except (TypeError, ValueError, OverflowError):
                durable_pnl = None
            if (
                claim.terminal_at_ms != at_ms
                or claim.terminal_reason != reason
                or not isinstance(result, Mapping)
                or result.get("risk_event_id") != event_id
                or durable_pnl != pnl
                or result.get("source_run_id") != run_id
                or bool(result.get("hard_loss_marker", False)) != hard_loss
            ):
                raise V1469PaidClaimConflictError(
                    "terminal paid close facts differ from durable claim"
                )
        event = DailyRiskEvent(
            event_id=event_id,
            occurred_at_ms=at_ms,
            fee_net_pnl_delta_usdc=pnl,
            risk_policy_hash=claim.risk_policy_hash,
        )
        inserted = await self._risk_repository.append_event(
            event,
            environment=claim.environment,
            symbol=claim.symbol,
            source_run_id=run_id,
            payload={
                "schema": "v1469.paid-close.1",
                "claim_id": claim.claim_id,
                "opportunity_id": claim.opportunity_id,
                "arm_key": claim.arm_key,
                "lease_id": claim.lease_id,
                "terminal_reason": reason,
                "hard_loss_marker": hard_loss,
            },
            created_at_ms=at_ms,
        )
        if claim.status == "TERMINAL":
            return PaidCloseResult(
                claim=claim,
                risk_event_inserted=inserted,
                terminal_mutation=None,
                replayed=True,
            )
        mutation = await self._claim_repository.terminalize_claim(
            claim_id=claim.claim_id,
            expected_generation=claim.generation,
            terminal_at_ms=at_ms,
            terminal_reason=reason,
            idempotency_key=f"terminal:{claim.claim_id}:{claim.generation}",
            actor=event_actor,
            result_payload={
                "schema": "v1469.paid-close.1",
                "outcome": "FILL",
                "risk_event_id": event_id,
                "fee_net_pnl_usdc": pnl,
                "source_run_id": run_id,
                "hard_loss_marker": hard_loss,
            },
        )
        return PaidCloseResult(
            claim=mutation.claim,
            risk_event_inserted=inserted,
            terminal_mutation=mutation,
            replayed=mutation.replayed,
        )

    async def record_no_fill(
        self,
        *,
        claim_id: str,
        terminal_reason: str,
        occurred_at_ms: int,
        source_run_id: str,
        actor: str = "v1469-paid-close-runtime",
    ) -> PaidNoFillResult:
        """Release a SUBMITTED reservation only after proven zero execution."""

        claim_key = str(claim_id or "").strip()
        reason = str(terminal_reason or "").strip()
        run_id = str(source_run_id or "").strip()
        event_actor = str(actor or "").strip()
        try:
            at_ms = int(occurred_at_ms)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("paid no-fill inputs are invalid") from exc
        if not claim_key or not reason or not run_id or not event_actor or at_ms < 0:
            raise ValueError("paid no-fill inputs are invalid")

        claim = await self._claim_repository.get_claim_by_id(claim_key)
        if claim is None:
            raise V1469PaidClaimConflictError("paid claim is missing")
        if claim.status not in {"SUBMITTED", "TERMINAL"}:
            raise V1469PaidClaimConflictError(
                "paid no-fill requires SUBMITTED or replayed TERMINAL claim"
            )
        result_payload = {
            "schema": "v1469.paid-no-fill.1",
            "outcome": "NO_FILL",
            "source_run_id": run_id,
        }
        if claim.status == "TERMINAL":
            if (
                claim.terminal_reason != reason
                or claim.result_payload != result_payload
            ):
                raise V1469PaidClaimConflictError(
                    "terminal paid no-fill facts differ from durable claim"
                )
            return PaidNoFillResult(
                claim=claim,
                terminal_mutation=None,
                replayed=True,
            )
        mutation = await self._claim_repository.terminalize_claim(
            claim_id=claim.claim_id,
            expected_generation=claim.generation,
            terminal_at_ms=at_ms,
            terminal_reason=reason,
            idempotency_key=f"terminal-no-fill:{claim.claim_id}:{claim.generation}",
            actor=event_actor,
            result_payload=result_payload,
        )
        return PaidNoFillResult(
            claim=mutation.claim,
            terminal_mutation=mutation,
            replayed=mutation.replayed,
        )


__all__ = [
    "PaidCloseResult",
    "PaidNoFillResult",
    "V1469PaidCloseRuntime",
]
