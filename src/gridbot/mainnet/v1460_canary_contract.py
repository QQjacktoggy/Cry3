"""Pure KPI and acceptance contract for the Codex v1.4.60 canary.

This module performs no I/O, owns no runtime state, and authorises no trading
action.  It only validates an already-aggregated reconciliation snapshot and
classifies the frozen 50 USDC, 20-fill canary.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import Enum
from typing import Final


class CanaryContractError(ValueError):
    """Raised when a snapshot or target violates the frozen contract."""


class CanaryStatus(str, Enum):
    """Closed set of v1.4.60 canary classifications."""

    ACTIVE = "ACTIVE"
    PASS = "PASS"
    STRETCH_PASS = "STRETCH_PASS"
    FAIL = "FAIL"
    FREQUENCY_INCONCLUSIVE = "FREQUENCY_INCONCLUSIVE"
    SAFETY_HALT = "SAFETY_HALT"


class CriterionName(str, Enum):
    """Stable names for every acceptance and reporting criterion."""

    EXACT_COMPLETE_PAID_CLOSED_FILLS = "exact_complete_paid_closed_fills"
    MINIMUM_WINS = "minimum_wins"
    RAW_WIN_RATE = "raw_win_rate"
    NET_PNL_USDC = "net_pnl_usdc"
    EV_PER_FILL = "ev_per_fill"
    REALIZED_EV_PER_DEDUP_OPPORTUNITY = (
        "realized_ev_per_dedup_incumbent_eligible_opportunity"
    )
    INTEGRITY = "integrity"
    STRETCH_NET_PNL_USDC = "stretch_net_pnl_usdc"


@dataclass(frozen=True, slots=True)
class CanaryTarget:
    """Immutable acceptance metadata; none of these values controls orders."""

    contract_version: str = "v1460-canary-kpi-v1"
    capital_usdc: Decimal = Decimal("50")
    complete_paid_closed_fills: int = 20
    minimum_wins: int = 15
    raw_win_rate_inclusive_minimum: Decimal = Decimal("0.75")
    net_pnl_usdc_exclusive_minimum: Decimal = Decimal("0")
    ev_per_fill_exclusive_minimum: Decimal = Decimal("0")
    realized_ev_per_dedup_opportunity_exclusive_minimum: Decimal = Decimal("0")
    stretch_net_pnl_usdc_exclusive_minimum: Decimal = Decimal("0.75")
    deadline_hours: int = 72


V1460_CANARY_TARGET: Final[CanaryTarget] = CanaryTarget()


@dataclass(frozen=True, slots=True)
class CanaryKpiSnapshot:
    """Aggregate input produced by an external exact reconciliation process.

    ``paid_closed_fills`` may count only COMPLETE paid closed fills.  It must
    exactly equal ``exact_reconciled_paid_closed_fills``.  Deduplicated
    incumbent-eligible opportunities include those fills and may additionally
    include no-fills; each no-fill contributes zero PnL through the denominator.
    """

    paid_closed_fills: int
    exact_reconciled_paid_closed_fills: int
    wins: int
    losses: int
    flats: int
    dedup_incumbent_eligible_opportunities: int
    net_pnl_usdc: Decimal
    incomplete_reconciliations: int = 0
    deadline_reached: bool = False
    safety_halt: bool = False
    safety_halt_reason: str | None = None

    def __post_init__(self) -> None:
        count_names = (
            "paid_closed_fills",
            "exact_reconciled_paid_closed_fills",
            "wins",
            "losses",
            "flats",
            "dedup_incumbent_eligible_opportunities",
            "incomplete_reconciliations",
        )
        for name in count_names:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CanaryContractError(f"{name} must be a non-negative integer")

        if not isinstance(self.deadline_reached, bool):
            raise CanaryContractError("deadline_reached must be boolean")
        if not isinstance(self.safety_halt, bool):
            raise CanaryContractError("safety_halt must be boolean")
        if not isinstance(self.net_pnl_usdc, Decimal):
            raise CanaryContractError("net_pnl_usdc must be Decimal")
        if not self.net_pnl_usdc.is_finite():
            raise CanaryContractError("net_pnl_usdc must be finite")

        target_fills = V1460_CANARY_TARGET.complete_paid_closed_fills
        if self.paid_closed_fills > target_fills:
            raise CanaryContractError("paid_closed_fills cannot exceed 20")
        if self.exact_reconciled_paid_closed_fills != self.paid_closed_fills:
            raise CanaryContractError(
                "all paid_closed_fills must be exact-reconciled COMPLETE fills"
            )
        if self.wins + self.losses + self.flats != self.paid_closed_fills:
            raise CanaryContractError(
                "wins, losses, and flats must sum to paid_closed_fills"
            )
        if self.dedup_incumbent_eligible_opportunities < self.paid_closed_fills:
            raise CanaryContractError(
                "dedup incumbent-eligible opportunities cannot be fewer than fills"
            )

        reason = self.safety_halt_reason
        if self.safety_halt:
            if not isinstance(reason, str) or not reason.strip():
                raise CanaryContractError(
                    "safety_halt_reason must be a non-empty string when safety_halt is true"
                )
        elif reason is not None:
            raise CanaryContractError(
                "safety_halt_reason must be None when safety_halt is false"
            )


CriterionActual = Decimal | int | bool


@dataclass(frozen=True, slots=True)
class CriterionResult:
    """Auditable actual, threshold, and decision for one criterion."""

    name: CriterionName
    met: bool
    actual: CriterionActual
    operator: str
    threshold: CriterionActual
    base_required: bool


@dataclass(frozen=True, slots=True)
class CanaryEvaluation:
    """Pure classification and the complete audit trail used to produce it."""

    status: CanaryStatus
    target: CanaryTarget
    snapshot: CanaryKpiSnapshot
    fills: CriterionResult
    wins: CriterionResult
    raw_win_rate: CriterionResult
    net_pnl_usdc: CriterionResult
    ev_per_fill: CriterionResult
    realized_ev_per_dedup_opportunity: CriterionResult
    integrity: CriterionResult
    stretch: CriterionResult
    wilson_95_lower_bound_report_only: Decimal

    @property
    def criteria(self) -> tuple[CriterionResult, ...]:
        """Return criteria in stable audit order; Wilson is intentionally absent."""

        return (
            self.fills,
            self.wins,
            self.raw_win_rate,
            self.net_pnl_usdc,
            self.ev_per_fill,
            self.realized_ev_per_dedup_opportunity,
            self.integrity,
            self.stretch,
        )

    @property
    def base_criteria(self) -> tuple[CriterionResult, ...]:
        """Return only criteria required for base PASS."""

        return tuple(criterion for criterion in self.criteria if criterion.base_required)


def _ratio(numerator: Decimal | int, denominator: int) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return Decimal(numerator) / Decimal(denominator)


def _wilson_95_lower_bound(wins: int, fills: int) -> Decimal:
    """Calculate the Wilson 95% lower bound for reporting only."""

    if fills == 0:
        return Decimal("0")

    with localcontext() as context:
        context.prec = 50
        z = Decimal("1.959963984540054")
        n = Decimal(fills)
        proportion = Decimal(wins) / n
        z_squared = z * z
        denominator = Decimal("1") + z_squared / n
        centre = proportion + z_squared / (Decimal("2") * n)
        margin = z * (
            (
                proportion * (Decimal("1") - proportion) / n
                + z_squared / (Decimal("4") * n * n)
            ).sqrt()
        )
        lower_bound = (centre - margin) / denominator
        return max(Decimal("0"), min(Decimal("1"), lower_bound))


def evaluate_canary(
    snapshot: CanaryKpiSnapshot,
    target: CanaryTarget = V1460_CANARY_TARGET,
) -> CanaryEvaluation:
    """Validate and classify a v1.4.60 canary KPI snapshot."""

    if target != V1460_CANARY_TARGET:
        raise CanaryContractError("the v1.4.60 canary target is immutable")

    raw_win_rate = _ratio(snapshot.wins, snapshot.paid_closed_fills)
    ev_per_fill = _ratio(snapshot.net_pnl_usdc, snapshot.paid_closed_fills)
    realized_ev_per_opportunity = _ratio(
        snapshot.net_pnl_usdc,
        snapshot.dedup_incumbent_eligible_opportunities,
    )
    integrity_met = (
        snapshot.incomplete_reconciliations == 0 and not snapshot.safety_halt
    )

    fills = CriterionResult(
        name=CriterionName.EXACT_COMPLETE_PAID_CLOSED_FILLS,
        met=snapshot.paid_closed_fills == target.complete_paid_closed_fills,
        actual=snapshot.paid_closed_fills,
        operator="==",
        threshold=target.complete_paid_closed_fills,
        base_required=True,
    )
    wins = CriterionResult(
        name=CriterionName.MINIMUM_WINS,
        met=snapshot.wins >= target.minimum_wins,
        actual=snapshot.wins,
        operator=">=",
        threshold=target.minimum_wins,
        base_required=True,
    )
    raw_win_rate_result = CriterionResult(
        name=CriterionName.RAW_WIN_RATE,
        met=raw_win_rate >= target.raw_win_rate_inclusive_minimum,
        actual=raw_win_rate,
        operator=">=",
        threshold=target.raw_win_rate_inclusive_minimum,
        base_required=True,
    )
    net_pnl = CriterionResult(
        name=CriterionName.NET_PNL_USDC,
        met=snapshot.net_pnl_usdc > target.net_pnl_usdc_exclusive_minimum,
        actual=snapshot.net_pnl_usdc,
        operator=">",
        threshold=target.net_pnl_usdc_exclusive_minimum,
        base_required=True,
    )
    ev_fill = CriterionResult(
        name=CriterionName.EV_PER_FILL,
        met=(
            snapshot.paid_closed_fills > 0
            and ev_per_fill > target.ev_per_fill_exclusive_minimum
        ),
        actual=ev_per_fill,
        operator=">",
        threshold=target.ev_per_fill_exclusive_minimum,
        base_required=True,
    )
    ev_opportunity = CriterionResult(
        name=CriterionName.REALIZED_EV_PER_DEDUP_OPPORTUNITY,
        met=(
            snapshot.dedup_incumbent_eligible_opportunities > 0
            and realized_ev_per_opportunity
            > target.realized_ev_per_dedup_opportunity_exclusive_minimum
        ),
        actual=realized_ev_per_opportunity,
        operator=">",
        threshold=target.realized_ev_per_dedup_opportunity_exclusive_minimum,
        base_required=True,
    )
    integrity = CriterionResult(
        name=CriterionName.INTEGRITY,
        met=integrity_met,
        actual=integrity_met,
        operator="is",
        threshold=True,
        base_required=True,
    )
    stretch = CriterionResult(
        name=CriterionName.STRETCH_NET_PNL_USDC,
        met=(
            snapshot.net_pnl_usdc
            > target.stretch_net_pnl_usdc_exclusive_minimum
        ),
        actual=snapshot.net_pnl_usdc,
        operator=">",
        threshold=target.stretch_net_pnl_usdc_exclusive_minimum,
        base_required=False,
    )

    criteria = (
        fills,
        wins,
        raw_win_rate_result,
        net_pnl,
        ev_fill,
        ev_opportunity,
        integrity,
    )
    if not integrity_met:
        status = CanaryStatus.SAFETY_HALT
    elif not fills.met:
        status = (
            CanaryStatus.FREQUENCY_INCONCLUSIVE
            if snapshot.deadline_reached
            else CanaryStatus.ACTIVE
        )
    elif all(criterion.met for criterion in criteria):
        status = CanaryStatus.STRETCH_PASS if stretch.met else CanaryStatus.PASS
    else:
        status = CanaryStatus.FAIL

    return CanaryEvaluation(
        status=status,
        target=target,
        snapshot=snapshot,
        fills=fills,
        wins=wins,
        raw_win_rate=raw_win_rate_result,
        net_pnl_usdc=net_pnl,
        ev_per_fill=ev_fill,
        realized_ev_per_dedup_opportunity=ev_opportunity,
        integrity=integrity,
        stretch=stretch,
        wilson_95_lower_bound_report_only=_wilson_95_lower_bound(
            snapshot.wins,
            snapshot.paid_closed_fills,
        ),
    )


__all__ = [
    "CanaryContractError",
    "CanaryEvaluation",
    "CanaryKpiSnapshot",
    "CanaryStatus",
    "CanaryTarget",
    "CriterionName",
    "CriterionResult",
    "V1460_CANARY_TARGET",
    "evaluate_canary",
]
