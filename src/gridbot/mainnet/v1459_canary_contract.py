"""Pure KPI contract for the Codex v1.4.59 live canary.

This module performs no I/O and authorises no trading action.  It only validates
an already-reconciled KPI snapshot and classifies its canary state.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Final


class CanaryContractError(ValueError):
    """Raised when a canary snapshot violates the accounting contract."""


class CanaryStatus(str, Enum):
    """Closed set of v1.4.59 canary outcomes."""

    ACTIVE = "ACTIVE"
    PASS = "PASS"
    HOLD = "HOLD"
    FREQUENCY_INCONCLUSIVE = "FREQUENCY_INCONCLUSIVE"


class CriterionName(str, Enum):
    """Stable names for every canary acceptance criterion."""

    EXACT_PAID_CLOSED_RUNS = "exact_paid_closed_runs"
    NET_PNL_USDC = "net_pnl_usdc"
    WIN_RATE = "win_rate"
    EV_PER_ACCEPTED_OPPORTUNITY = "ev_per_accepted_opportunity"


@dataclass(frozen=True, slots=True)
class CanaryTarget:
    """Immutable target metadata; these values are not runtime controls."""

    # v2 freezes paid-closed-fill semantics: no-fill attempts cannot consume
    # the 20-fill target or enter WR/PnL accounting.
    contract_version: str = "v1459-canary-kpi-v2"
    capital_usdc: Decimal = Decimal("50")
    paid_closed_fills: int = 20
    net_pnl_usdc_exclusive_minimum: Decimal = Decimal("0.75")
    win_rate_exclusive_minimum: Decimal = Decimal("0.70")
    ev_per_accepted_opportunity_exclusive_minimum: Decimal = Decimal("0")
    deadline_hours: int = 72


V1459_CANARY_TARGET: Final[CanaryTarget] = CanaryTarget()


@dataclass(frozen=True, slots=True)
class CanaryKpiSnapshot:
    """One outcome-blind aggregate supplied by exact reconciliation.

    ``paid_closed_fills`` may contain only COMPLETE, exact-reconciled closed
    runs.  Incomplete reconciliations are tracked separately and never enter
    wins, losses, flats, PnL, or the paid-fill target.
    """

    paid_closed_fills: int
    exact_reconciled_paid_closed_fills: int
    wins: int
    losses: int
    flats: int
    accepted_opportunities: int
    net_pnl_usdc: Decimal
    incomplete_reconciliations: int = 0
    deadline_reached: bool = False

    def __post_init__(self) -> None:
        count_names = (
            "paid_closed_fills",
            "exact_reconciled_paid_closed_fills",
            "wins",
            "losses",
            "flats",
            "accepted_opportunities",
            "incomplete_reconciliations",
        )
        for name in count_names:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CanaryContractError(f"{name} must be a non-negative integer")

        if not isinstance(self.deadline_reached, bool):
            raise CanaryContractError("deadline_reached must be boolean")
        if not isinstance(self.net_pnl_usdc, Decimal):
            raise CanaryContractError("net_pnl_usdc must be Decimal")
        if not self.net_pnl_usdc.is_finite():
            raise CanaryContractError("net_pnl_usdc must be finite")
        if self.paid_closed_fills > V1459_CANARY_TARGET.paid_closed_fills:
            raise CanaryContractError("paid_closed_fills cannot exceed the target")
        if (
            self.exact_reconciled_paid_closed_fills
            != self.paid_closed_fills
        ):
            raise CanaryContractError(
                "paid_closed_fills must all be exact-reconciled COMPLETE runs"
            )
        if self.wins + self.losses + self.flats != self.paid_closed_fills:
            raise CanaryContractError(
                "wins, losses, and flats must sum to paid_closed_fills"
            )
        if self.paid_closed_fills > 0 and self.accepted_opportunities == 0:
            raise CanaryContractError(
                "accepted_opportunities must be positive when paid fills exist"
            )


@dataclass(frozen=True, slots=True)
class CriterionResult:
    """Auditable result for one strict canary criterion."""

    name: CriterionName
    met: bool
    actual: Decimal | int
    operator: str
    threshold: Decimal | int


@dataclass(frozen=True, slots=True)
class CanaryEvaluation:
    """Pure classification plus every criterion used to reach it."""

    status: CanaryStatus
    target: CanaryTarget
    paid_closed_runs: CriterionResult
    net_pnl_usdc: CriterionResult
    win_rate: CriterionResult
    ev_per_accepted_opportunity: CriterionResult

    @property
    def criteria(self) -> tuple[CriterionResult, ...]:
        return (
            self.paid_closed_runs,
            self.net_pnl_usdc,
            self.win_rate,
            self.ev_per_accepted_opportunity,
        )


def evaluate_canary(
    snapshot: CanaryKpiSnapshot,
    target: CanaryTarget = V1459_CANARY_TARGET,
) -> CanaryEvaluation:
    """Validate and classify a v1.4.59 canary KPI snapshot."""

    if target != V1459_CANARY_TARGET:
        raise CanaryContractError("the v1.4.59 canary target is immutable")

    paid_target_met = snapshot.paid_closed_fills == target.paid_closed_fills
    paid = CriterionResult(
        name=CriterionName.EXACT_PAID_CLOSED_RUNS,
        met=paid_target_met,
        actual=snapshot.paid_closed_fills,
        operator="==",
        threshold=target.paid_closed_fills,
    )

    net_met = snapshot.net_pnl_usdc > target.net_pnl_usdc_exclusive_minimum
    net = CriterionResult(
        name=CriterionName.NET_PNL_USDC,
        met=net_met,
        actual=snapshot.net_pnl_usdc,
        operator=">",
        threshold=target.net_pnl_usdc_exclusive_minimum,
    )

    win_rate = (
        Decimal(snapshot.wins) / Decimal(snapshot.paid_closed_fills)
        if snapshot.paid_closed_fills
        else Decimal("0")
    )
    win_rate_met = win_rate > target.win_rate_exclusive_minimum
    win_rate_result = CriterionResult(
        name=CriterionName.WIN_RATE,
        met=win_rate_met,
        actual=win_rate,
        operator=">",
        threshold=target.win_rate_exclusive_minimum,
    )

    ev = (
        snapshot.net_pnl_usdc / Decimal(snapshot.accepted_opportunities)
        if snapshot.accepted_opportunities
        else Decimal("0")
    )
    ev_met = (
        snapshot.accepted_opportunities > 0
        and ev > target.ev_per_accepted_opportunity_exclusive_minimum
    )
    ev_result = CriterionResult(
        name=CriterionName.EV_PER_ACCEPTED_OPPORTUNITY,
        met=ev_met,
        actual=ev,
        operator=">",
        threshold=target.ev_per_accepted_opportunity_exclusive_minimum,
    )

    if not paid_target_met:
        status = (
            CanaryStatus.FREQUENCY_INCONCLUSIVE
            if snapshot.deadline_reached
            else CanaryStatus.ACTIVE
        )
    elif net_met and win_rate_met and ev_met:
        status = CanaryStatus.PASS
    else:
        status = CanaryStatus.HOLD

    return CanaryEvaluation(
        status=status,
        target=target,
        paid_closed_runs=paid,
        net_pnl_usdc=net,
        win_rate=win_rate_result,
        ev_per_accepted_opportunity=ev_result,
    )


__all__ = [
    "CanaryContractError",
    "CanaryEvaluation",
    "CanaryKpiSnapshot",
    "CanaryStatus",
    "CanaryTarget",
    "CriterionName",
    "CriterionResult",
    "V1459_CANARY_TARGET",
    "evaluate_canary",
]
