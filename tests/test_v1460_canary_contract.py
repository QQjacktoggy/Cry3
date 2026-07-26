from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from src.gridbot.mainnet.v1460_canary_contract import (
    CanaryContractError,
    CanaryKpiSnapshot,
    CanaryStatus,
    CanaryTarget,
    CriterionName,
    V1460_CANARY_TARGET,
    evaluate_canary,
)


def _snapshot(**overrides: object) -> CanaryKpiSnapshot:
    values: dict[str, object] = {
        "paid_closed_fills": 20,
        "exact_reconciled_paid_closed_fills": 20,
        "wins": 15,
        "losses": 5,
        "flats": 0,
        "dedup_incumbent_eligible_opportunities": 25,
        "net_pnl_usdc": Decimal("0.50"),
        "incomplete_reconciliations": 0,
        "deadline_reached": False,
        "safety_halt": False,
        "safety_halt_reason": None,
    }
    values.update(overrides)
    return CanaryKpiSnapshot(**values)  # type: ignore[arg-type]


def test_status_enum_is_the_exact_frozen_set() -> None:
    assert tuple(status.value for status in CanaryStatus) == (
        "ACTIVE",
        "PASS",
        "STRETCH_PASS",
        "FAIL",
        "FREQUENCY_INCONCLUSIVE",
        "SAFETY_HALT",
    )


def test_target_is_exact_and_immutable() -> None:
    target = V1460_CANARY_TARGET
    assert target.contract_version == "v1460-canary-kpi-v1"
    assert target.capital_usdc == Decimal("50")
    assert target.complete_paid_closed_fills == 20
    assert target.minimum_wins == 15
    assert target.raw_win_rate_inclusive_minimum == Decimal("0.75")
    assert target.net_pnl_usdc_exclusive_minimum == Decimal("0")
    assert target.ev_per_fill_exclusive_minimum == Decimal("0")
    assert (
        target.realized_ev_per_dedup_opportunity_exclusive_minimum
        == Decimal("0")
    )
    assert target.stretch_net_pnl_usdc_exclusive_minimum == Decimal("0.75")
    assert target.deadline_hours == 72

    with pytest.raises(FrozenInstanceError):
        target.capital_usdc = Decimal("100")  # type: ignore[misc]
    with pytest.raises(CanaryContractError, match="target is immutable"):
        evaluate_canary(
            _snapshot(),
            replace(CanaryTarget(), capital_usdc=Decimal("51")),
        )


def test_snapshot_and_evaluation_are_immutable() -> None:
    snapshot = _snapshot()
    evaluation = evaluate_canary(snapshot)

    with pytest.raises(FrozenInstanceError):
        snapshot.wins = 20  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        evaluation.status = CanaryStatus.FAIL  # type: ignore[misc]


def test_base_pass_at_fifteen_of_twenty_with_positive_ev() -> None:
    result = evaluate_canary(_snapshot())

    assert result.status is CanaryStatus.PASS
    assert result.raw_win_rate.actual == Decimal("0.75")
    assert result.ev_per_fill.actual == Decimal("0.025")
    assert result.realized_ev_per_dedup_opportunity.actual == Decimal("0.02")
    assert result.integrity.met is True
    assert result.stretch.met is False
    assert all(criterion.met for criterion in result.base_criteria)


def test_criteria_are_complete_auditable_and_stably_ordered() -> None:
    result = evaluate_canary(_snapshot())

    assert tuple(criterion.name for criterion in result.criteria) == (
        CriterionName.EXACT_COMPLETE_PAID_CLOSED_FILLS,
        CriterionName.MINIMUM_WINS,
        CriterionName.RAW_WIN_RATE,
        CriterionName.NET_PNL_USDC,
        CriterionName.EV_PER_FILL,
        CriterionName.REALIZED_EV_PER_DEDUP_OPPORTUNITY,
        CriterionName.INTEGRITY,
        CriterionName.STRETCH_NET_PNL_USDC,
    )
    assert tuple(criterion.name for criterion in result.base_criteria) == tuple(
        criterion.name for criterion in result.criteria[:-1]
    )
    assert result.fills.operator == "=="
    assert result.wins.operator == ">="
    assert result.raw_win_rate.operator == ">="
    assert result.net_pnl_usdc.operator == ">"
    assert result.ev_per_fill.operator == ">"
    assert result.realized_ev_per_dedup_opportunity.operator == ">"
    assert result.integrity.actual is True
    assert result.integrity.threshold is True
    assert result.stretch.base_required is False
    assert result.snapshot == _snapshot()


def test_wilson_lower_bound_is_report_only_and_not_a_conservative_pass_label() -> None:
    result = evaluate_canary(_snapshot())

    assert Decimal("0.53") < result.wilson_95_lower_bound_report_only < Decimal(
        "0.54"
    )
    assert result.wilson_95_lower_bound_report_only < Decimal("0.70")
    assert result.status is CanaryStatus.PASS
    assert all("wilson" not in criterion.name.value for criterion in result.criteria)


def test_zero_fills_has_zero_raw_rate_ev_and_wilson() -> None:
    result = evaluate_canary(
        _snapshot(
            paid_closed_fills=0,
            exact_reconciled_paid_closed_fills=0,
            wins=0,
            losses=0,
            dedup_incumbent_eligible_opportunities=0,
            net_pnl_usdc=Decimal("0"),
        )
    )

    assert result.status is CanaryStatus.ACTIVE
    assert result.raw_win_rate.actual == Decimal("0")
    assert result.ev_per_fill.actual == Decimal("0")
    assert result.realized_ev_per_dedup_opportunity.actual == Decimal("0")
    assert result.wilson_95_lower_bound_report_only == Decimal("0")


def test_stretch_requires_strictly_more_than_point_seventy_five() -> None:
    exact_boundary = evaluate_canary(_snapshot(net_pnl_usdc=Decimal("0.75")))
    above_boundary = evaluate_canary(
        _snapshot(net_pnl_usdc=Decimal("0.75000001"))
    )

    assert exact_boundary.status is CanaryStatus.PASS
    assert exact_boundary.stretch.met is False
    assert above_boundary.status is CanaryStatus.STRETCH_PASS
    assert above_boundary.stretch.met is True


def test_stretch_does_not_rescue_failed_base_criteria() -> None:
    result = evaluate_canary(
        _snapshot(wins=14, losses=6, net_pnl_usdc=Decimal("1.00"))
    )

    assert result.stretch.met is True
    assert result.status is CanaryStatus.FAIL


def test_fourteen_of_twenty_is_raw_seventy_percent_and_fails() -> None:
    result = evaluate_canary(_snapshot(wins=14, losses=6))

    assert result.status is CanaryStatus.FAIL
    assert result.wins.met is False
    assert result.raw_win_rate.actual == Decimal("0.70")
    assert result.raw_win_rate.met is False


def test_fifteen_wins_with_a_flat_still_meets_raw_win_rate() -> None:
    result = evaluate_canary(_snapshot(wins=15, losses=4, flats=1))

    assert result.status is CanaryStatus.PASS
    assert result.raw_win_rate.actual == Decimal("0.75")


@pytest.mark.parametrize("net_pnl", (Decimal("0"), Decimal("-0.00000001")))
def test_non_positive_net_and_ev_fail(net_pnl: Decimal) -> None:
    result = evaluate_canary(_snapshot(net_pnl_usdc=net_pnl))

    assert result.status is CanaryStatus.FAIL
    assert result.net_pnl_usdc.met is False
    assert result.ev_per_fill.met is False
    assert result.realized_ev_per_dedup_opportunity.met is False


def test_arbitrarily_small_positive_net_meets_base_profit_and_ev() -> None:
    result = evaluate_canary(_snapshot(net_pnl_usdc=Decimal("0.00000001")))

    assert result.status is CanaryStatus.PASS
    assert result.net_pnl_usdc.met is True
    assert result.ev_per_fill.met is True
    assert result.realized_ev_per_dedup_opportunity.met is True


def test_no_fill_opportunities_contribute_zero_through_denominator() -> None:
    result = evaluate_canary(
        _snapshot(
            dedup_incumbent_eligible_opportunities=40,
            net_pnl_usdc=Decimal("1"),
        )
    )

    assert result.ev_per_fill.actual == Decimal("0.05")
    assert result.realized_ev_per_dedup_opportunity.actual == Decimal("0.025")


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
    assert result.fills.met is False


def test_under_twenty_is_frequency_inconclusive_at_deadline() -> None:
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


def test_deadline_does_not_override_final_classification_at_twenty() -> None:
    passed = evaluate_canary(_snapshot(deadline_reached=True))
    failed = evaluate_canary(
        _snapshot(wins=14, losses=6, deadline_reached=True)
    )

    assert passed.status is CanaryStatus.PASS
    assert failed.status is CanaryStatus.FAIL


def test_incomplete_reconciliation_has_highest_precedence() -> None:
    result = evaluate_canary(
        _snapshot(incomplete_reconciliations=1, deadline_reached=True)
    )

    assert result.status is CanaryStatus.SAFETY_HALT
    assert result.integrity.met is False
    assert result.integrity.actual is False


def test_explicit_safety_halt_has_highest_precedence() -> None:
    result = evaluate_canary(
        _snapshot(
            paid_closed_fills=19,
            exact_reconciled_paid_closed_fills=19,
            wins=15,
            losses=4,
            deadline_reached=True,
            safety_halt=True,
            safety_halt_reason="identity mismatch",
        )
    )

    assert result.status is CanaryStatus.SAFETY_HALT
    assert result.snapshot.safety_halt_reason == "identity mismatch"
    assert result.integrity.met is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"paid_closed_fills": -1}, "non-negative integer"),
        ({"exact_reconciled_paid_closed_fills": -1}, "non-negative integer"),
        ({"wins": -1}, "non-negative integer"),
        ({"losses": -1}, "non-negative integer"),
        ({"flats": -1}, "non-negative integer"),
        ({"dedup_incumbent_eligible_opportunities": -1}, "non-negative integer"),
        ({"incomplete_reconciliations": -1}, "non-negative integer"),
        ({"wins": True}, "non-negative integer"),
        ({"losses": 5.0}, "non-negative integer"),
        (
            {
                "paid_closed_fills": 21,
                "exact_reconciled_paid_closed_fills": 21,
                "wins": 16,
                "losses": 5,
            },
            "cannot exceed 20",
        ),
        ({"exact_reconciled_paid_closed_fills": 19}, "exact-reconciled COMPLETE"),
        ({"wins": 14}, "must sum"),
        ({"dedup_incumbent_eligible_opportunities": 19}, "cannot be fewer"),
    ),
)
def test_invalid_counts_fail_closed(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(CanaryContractError, match=message):
        _snapshot(**overrides)


@pytest.mark.parametrize(
    "invalid_net",
    (0.5, Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")),
)
def test_net_pnl_requires_a_finite_decimal(invalid_net: object) -> None:
    message = "must be Decimal" if not isinstance(invalid_net, Decimal) else "must be finite"
    with pytest.raises(CanaryContractError, match=message):
        _snapshot(net_pnl_usdc=invalid_net)


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"deadline_reached": 1}, "deadline_reached must be boolean"),
        ({"safety_halt": 1}, "safety_halt must be boolean"),
        ({"safety_halt": True}, "must be a non-empty string"),
        (
            {"safety_halt": True, "safety_halt_reason": ""},
            "must be a non-empty string",
        ),
        (
            {"safety_halt": True, "safety_halt_reason": "   "},
            "must be a non-empty string",
        ),
        (
            {"safety_halt": True, "safety_halt_reason": 42},
            "must be a non-empty string",
        ),
        ({"safety_halt_reason": "stale"}, "must be None"),
        ({"safety_halt_reason": ""}, "must be None"),
    ),
)
def test_boolean_and_safety_reason_consistency_fail_closed(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(CanaryContractError, match=message):
        _snapshot(**overrides)
