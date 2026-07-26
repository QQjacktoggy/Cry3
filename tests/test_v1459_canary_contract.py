from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from src.gridbot.mainnet.v1459_canary_contract import (
    CanaryContractError,
    CanaryKpiSnapshot,
    CanaryStatus,
    CanaryTarget,
    CriterionName,
    V1459_CANARY_TARGET,
    evaluate_canary,
)


def _snapshot(**overrides: object) -> CanaryKpiSnapshot:
    values: dict[str, object] = {
        "paid_closed_fills": 20,
        "exact_reconciled_paid_closed_fills": 20,
        "wins": 15,
        "losses": 5,
        "flats": 0,
        "accepted_opportunities": 25,
        "net_pnl_usdc": Decimal("0.76"),
        "incomplete_reconciliations": 0,
        "deadline_reached": False,
    }
    values.update(overrides)
    return CanaryKpiSnapshot(**values)  # type: ignore[arg-type]


def test_target_metadata_is_exact_and_immutable() -> None:
    assert V1459_CANARY_TARGET.contract_version == "v1459-canary-kpi-v2"
    assert V1459_CANARY_TARGET.capital_usdc == Decimal("50")
    assert V1459_CANARY_TARGET.paid_closed_fills == 20
    assert V1459_CANARY_TARGET.net_pnl_usdc_exclusive_minimum == Decimal("0.75")
    assert V1459_CANARY_TARGET.win_rate_exclusive_minimum == Decimal("0.70")
    assert V1459_CANARY_TARGET.deadline_hours == 72
    with pytest.raises(FrozenInstanceError):
        V1459_CANARY_TARGET.capital_usdc = Decimal("100")  # type: ignore[misc]
    with pytest.raises(CanaryContractError, match="target is immutable"):
        evaluate_canary(_snapshot(), replace(CanaryTarget(), capital_usdc=Decimal("51")))


def test_fifteen_of_twenty_and_all_strict_thresholds_pass() -> None:
    result = evaluate_canary(_snapshot())
    assert result.status is CanaryStatus.PASS
    assert all(criterion.met for criterion in result.criteria)
    assert result.win_rate.actual == Decimal("0.75")
    assert result.ev_per_accepted_opportunity.actual == Decimal("0.0304")
    assert tuple(item.name for item in result.criteria) == (
        CriterionName.EXACT_PAID_CLOSED_RUNS,
        CriterionName.NET_PNL_USDC,
        CriterionName.WIN_RATE,
        CriterionName.EV_PER_ACCEPTED_OPPORTUNITY,
    )


def test_fourteen_of_twenty_is_exactly_seventy_percent_and_holds() -> None:
    result = evaluate_canary(_snapshot(wins=14, losses=6))
    assert result.status is CanaryStatus.HOLD
    assert result.win_rate.actual == Decimal("0.70")
    assert result.win_rate.met is False


def test_net_exactly_point_seventy_five_fails_strict_threshold() -> None:
    result = evaluate_canary(_snapshot(net_pnl_usdc=Decimal("0.75")))
    assert result.status is CanaryStatus.HOLD
    assert result.net_pnl_usdc.met is False
    assert result.net_pnl_usdc.operator == ">"


def test_zero_ev_fails_strict_threshold() -> None:
    result = evaluate_canary(_snapshot(net_pnl_usdc=Decimal("0")))
    assert result.status is CanaryStatus.HOLD
    assert result.ev_per_accepted_opportunity.actual == Decimal("0")
    assert result.ev_per_accepted_opportunity.met is False


def test_under_twenty_is_active_before_deadline() -> None:
    result = evaluate_canary(
        _snapshot(
            paid_closed_fills=19,
            exact_reconciled_paid_closed_fills=19,
            wins=15,
            losses=4,
        )
    )
    assert result.status is CanaryStatus.ACTIVE
    assert result.paid_closed_runs.met is False


def test_under_twenty_at_deadline_is_frequency_inconclusive() -> None:
    result = evaluate_canary(
        _snapshot(
            paid_closed_fills=19,
            exact_reconciled_paid_closed_fills=19,
            wins=15,
            losses=4,
            deadline_reached=True,
        )
    )
    assert result.status is CanaryStatus.FREQUENCY_INCONCLUSIVE


def test_incomplete_reconciliations_are_observed_but_never_counted() -> None:
    result = evaluate_canary(
        _snapshot(
            paid_closed_fills=0,
            exact_reconciled_paid_closed_fills=0,
            wins=0,
            losses=0,
            incomplete_reconciliations=7,
            accepted_opportunities=7,
            net_pnl_usdc=Decimal("0"),
        )
    )
    assert result.status is CanaryStatus.ACTIVE
    assert result.paid_closed_runs.actual == 0
    assert result.win_rate.actual == Decimal("0")


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"wins": 14}, "must sum"),
        ({"losses": -1, "flats": 1}, "non-negative integer"),
        ({"paid_closed_fills": 21, "exact_reconciled_paid_closed_fills": 21, "wins": 16, "losses": 5}, "cannot exceed"),
        ({"exact_reconciled_paid_closed_fills": 19}, "exact-reconciled COMPLETE"),
        ({"accepted_opportunities": -1}, "non-negative integer"),
        ({"accepted_opportunities": 0}, "must be positive"),
        ({"paid_closed_fills": True}, "non-negative integer"),
        ({"net_pnl_usdc": Decimal("NaN")}, "must be finite"),
    ),
)
def test_illegal_counts_and_values_fail_closed(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(CanaryContractError, match=message):
        _snapshot(**overrides)


def test_net_pnl_requires_decimal_to_avoid_float_boundary_drift() -> None:
    with pytest.raises(CanaryContractError, match="must be Decimal"):
        _snapshot(net_pnl_usdc=0.76)
