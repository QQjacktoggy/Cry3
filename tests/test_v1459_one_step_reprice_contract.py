from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from decimal import ROUND_DOWN, ROUND_UP, Decimal, localcontext
from itertools import permutations

import pytest

from src.gridbot.mainnet.v1459_one_step_reprice_contract import (
    ONE_STEP_REPRICE_POLICY_HASH,
    FrozenMenuStep,
    FrozenPriceMenu,
    OneStepRepriceContract,
    OrderSide,
    ReplaceRequest,
    RepriceContractError,
    RepriceEvent,
    RepriceReason,
    RepriceState,
    SafetyFacts,
    transition_one_step_reprice,
)


SAFE = SafetyFacts(True, True, True)


def _menu(side: OrderSide = OrderSide.BUY) -> FrozenPriceMenu:
    if side is OrderSide.BUY:
        return FrozenPriceMenu(
            side=side,
            e2=Decimal("99.98"),
            e1=Decimal("99.99"),
            e0=Decimal("100.00"),
        )
    return FrozenPriceMenu(
        side=side,
        e2=Decimal("100.02"),
        e1=Decimal("100.01"),
        e0=Decimal("100.00"),
    )


def _open(
    *,
    side: OrderSide = OrderSide.BUY,
    step: FrozenMenuStep = FrozenMenuStep.E2,
    deadline: int = 2_000,
) -> OneStepRepriceContract:
    return OneStepRepriceContract.open(
        opportunity_id="opp-1",
        incumbent_order_id="order-1",
        opened_at_ms=1_000,
        ttl_deadline_ms=deadline,
        frozen_menu=_menu(side),
        incumbent_step=step,
    )


def _cancel_event(at: int = 1_100, safety: SafetyFacts = SAFE) -> RepriceEvent:
    return RepriceEvent.request_cancel(observed_at_ms=at, safety=safety)


def _confirm_event(
    at: int = 1_200,
    *,
    confirmed: bool = True,
    order_id: str | None = "order-1",
    safety: SafetyFacts = SAFE,
) -> RepriceEvent:
    return RepriceEvent.confirm_cancel(
        observed_at_ms=at,
        safety=safety,
        cancel_confirmed=confirmed,
        confirmed_order_id=order_id,
    )


def _replace_event(
    at: int = 1_300,
    *,
    step: FrozenMenuStep = FrozenMenuStep.E1,
    best_bid: str = "99.95",
    best_ask: str = "100.01",
    post_only: bool = True,
    allow_taker: bool = False,
    safety: SafetyFacts = SAFE,
) -> RepriceEvent:
    return RepriceEvent.request_replace(
        observed_at_ms=at,
        safety=safety,
        replacement=ReplaceRequest(
            requested_step=step,
            best_bid=Decimal(best_bid),
            best_ask=Decimal(best_ask),
            post_only=post_only,
            allow_taker=allow_taker,
        ),
    )


def _cancel_confirmed(
    *,
    side: OrderSide = OrderSide.BUY,
    step: FrozenMenuStep = FrozenMenuStep.E2,
) -> OneStepRepriceContract:
    snapshot = _open(side=side, step=step)
    snapshot = snapshot.transition(_cancel_event())
    return snapshot.transition(_confirm_event())


def _happy_path() -> tuple[OneStepRepriceContract, ...]:
    incumbent = _open()
    requested = incumbent.transition(_cancel_event())
    confirmed = requested.transition(_confirm_event())
    allowed = transition_one_step_reprice(confirmed, _replace_event())
    return incumbent, requested, confirmed, allowed


def test_exact_ordered_path_produces_one_post_only_adjacent_intent() -> None:
    incumbent, requested, confirmed, allowed = _happy_path()

    assert [
        incumbent.state,
        requested.state,
        confirmed.state,
        allowed.state,
    ] == [
        RepriceState.INCUMBENT_OPEN,
        RepriceState.CANCEL_REQUESTED,
        RepriceState.CANCEL_CONFIRMED,
        RepriceState.REPLACE_ALLOWED,
    ]
    assert [
        incumbent.reason,
        requested.reason,
        confirmed.reason,
        allowed.reason,
    ] == [
        RepriceReason.INCUMBENT_OPENED,
        RepriceReason.CANCEL_REQUEST_ACCEPTED,
        RepriceReason.CANCEL_CONFIRMATION_ACCEPTED,
        RepriceReason.REPLACE_INTENT_ALLOWED,
    ]
    assert allowed.cancel_confirmed is True
    assert allowed.reprice_count == 1
    assert allowed.replace_allowed is True
    assert allowed.price_intent is not None
    assert allowed.price_intent.from_step is FrozenMenuStep.E2
    assert allowed.price_intent.to_step is FrozenMenuStep.E1
    assert allowed.price_intent.limit_price == Decimal("99.99")
    assert allowed.price_intent.post_only is True
    assert allowed.price_intent.allow_taker is False


def test_every_event_permutation_except_required_order_fails_closed() -> None:
    event_builders = (_cancel_event, _confirm_event, _replace_event)

    for ordered_builders in permutations(event_builders):
        snapshot = _open()
        for builder in ordered_builders:
            snapshot = snapshot.transition(builder())
        if ordered_builders == event_builders:
            assert snapshot.state is RepriceState.REPLACE_ALLOWED
        else:
            assert snapshot.state is RepriceState.EXPIRE
            assert snapshot.replace_allowed is False


def test_replace_never_becomes_allowed_without_positive_matching_confirmation() -> None:
    requested = _open().transition(_cancel_event())

    no_confirmation = requested.transition(_replace_event())
    assert no_confirmation.state is RepriceState.EXPIRE
    assert no_confirmation.reason is RepriceReason.CANCEL_NOT_CONFIRMED
    assert no_confirmation.reprice_count == 0
    assert no_confirmation.price_intent is None

    negative_confirmation = requested.transition(
        _confirm_event(confirmed=False, order_id=None)
    )
    assert negative_confirmation.state is RepriceState.EXPIRE
    assert negative_confirmation.reason is RepriceReason.CANCEL_NOT_CONFIRMED

    wrong_order = requested.transition(_confirm_event(order_id="another-order"))
    assert wrong_order.state is RepriceState.EXPIRE
    assert wrong_order.reason is RepriceReason.CANCEL_ORDER_MISMATCH


@pytest.mark.parametrize(
    ("safety", "reason"),
    [
        (SafetyFacts(False, True, True), RepriceReason.OWNERSHIP_FAILED),
        (SafetyFacts(None, True, True), RepriceReason.OWNERSHIP_FAILED),
        (SafetyFacts(True, False, True), RepriceReason.SIGNAL_FAILED),
        (SafetyFacts(True, None, True), RepriceReason.SIGNAL_FAILED),
        (SafetyFacts(True, True, False), RepriceReason.SPREAD_FAILED),
        (SafetyFacts(True, True, None), RepriceReason.SPREAD_FAILED),
    ],
)
def test_any_unproven_safety_fact_expires(
    safety: SafetyFacts, reason: RepriceReason
) -> None:
    expired = _open().transition(_cancel_event(safety=safety))
    assert expired.state is RepriceState.EXPIRE
    assert expired.reason is reason
    assert expired.reprice_count == 0


def test_safety_is_rechecked_after_cancel_confirmation() -> None:
    confirmed = _cancel_confirmed()
    expired = confirmed.transition(
        _replace_event(safety=SafetyFacts(True, False, True))
    )
    assert expired.state is RepriceState.EXPIRE
    assert expired.reason is RepriceReason.SIGNAL_FAILED
    assert expired.price_intent is None


def test_ttl_deadline_is_immutable_and_exact_deadline_expires() -> None:
    snapshots = _happy_path()
    assert {snapshot.ttl_deadline_ms for snapshot in snapshots} == {2_000}
    assert snapshots[-1].price_intent is not None
    assert snapshots[-1].price_intent.ttl_deadline_ms == 2_000
    assert "ttl_deadline_ms" not in {field.name for field in fields(RepriceEvent)}

    with pytest.raises(FrozenInstanceError):
        snapshots[0].ttl_deadline_ms = 3_000  # type: ignore[misc]

    at_deadline = _open().transition(_cancel_event(at=2_000))
    assert at_deadline.state is RepriceState.EXPIRE
    assert at_deadline.reason is RepriceReason.TTL_EXPIRED
    assert at_deadline.ttl_deadline_ms == 2_000


def test_observation_time_cannot_move_backwards() -> None:
    requested = _open().transition(_cancel_event(at=1_200))
    expired = requested.transition(_confirm_event(at=1_199))
    assert expired.state is RepriceState.EXPIRE
    assert expired.reason is RepriceReason.OBSERVATION_TIME_REGRESSION


def test_only_the_immediately_closer_frozen_step_is_accepted() -> None:
    skipped = _cancel_confirmed().transition(
        _replace_event(step=FrozenMenuStep.E0)
    )
    assert skipped.state is RepriceState.EXPIRE
    assert skipped.reason is RepriceReason.NOT_ONE_FROZEN_STEP

    from_e1 = _cancel_confirmed(step=FrozenMenuStep.E1).transition(
        _replace_event(step=FrozenMenuStep.E0)
    )
    assert from_e1.state is RepriceState.REPLACE_ALLOWED
    assert from_e1.price_intent is not None
    assert from_e1.price_intent.limit_price == Decimal("100.00")

    from_e0 = _cancel_confirmed(step=FrozenMenuStep.E0).transition(
        _replace_event(step=FrozenMenuStep.E0)
    )
    assert from_e0.state is RepriceState.EXPIRE
    assert from_e0.reason is RepriceReason.NO_CLOSER_FROZEN_STEP


@pytest.mark.parametrize(
    ("post_only", "allow_taker"),
    [(False, False), (True, True), (False, True)],
)
def test_taker_or_non_post_only_request_is_forbidden(
    post_only: bool, allow_taker: bool
) -> None:
    expired = _cancel_confirmed().transition(
        _replace_event(post_only=post_only, allow_taker=allow_taker)
    )
    assert expired.state is RepriceState.EXPIRE
    assert expired.reason is RepriceReason.TAKER_FORBIDDEN
    assert expired.price_intent is None


@pytest.mark.parametrize(
    ("side", "best_bid", "best_ask"),
    [
        (OrderSide.BUY, "99.98", "99.99"),
        (OrderSide.SELL, "100.01", "100.02"),
    ],
)
def test_crossing_or_touching_opposite_quote_is_forbidden(
    side: OrderSide, best_bid: str, best_ask: str
) -> None:
    expired = _cancel_confirmed(side=side).transition(
        _replace_event(best_bid=best_bid, best_ask=best_ask)
    )
    assert expired.state is RepriceState.EXPIRE
    assert expired.reason is RepriceReason.PRICE_WOULD_CROSS
    assert expired.price_intent is None


def test_sell_intent_moves_one_step_closer_without_crossing() -> None:
    allowed = _cancel_confirmed(side=OrderSide.SELL).transition(
        _replace_event(best_bid="100.00", best_ask="100.02")
    )
    assert allowed.state is RepriceState.REPLACE_ALLOWED
    assert allowed.price_intent is not None
    assert allowed.price_intent.side is OrderSide.SELL
    assert allowed.price_intent.limit_price == Decimal("100.01")
    assert allowed.price_intent.limit_price > allowed.price_intent.observed_best_bid


def test_reprice_can_be_consumed_at_most_once() -> None:
    allowed = _happy_path()[-1]
    duplicate = allowed.transition(_replace_event(at=1_400))
    assert duplicate.state is RepriceState.EXPIRE
    assert duplicate.reason is RepriceReason.REPRICE_ALREADY_USED
    assert duplicate.reprice_count == 1
    assert duplicate.price_intent == allowed.price_intent
    assert duplicate.replace_allowed is False


def test_expire_is_terminal_and_idempotent() -> None:
    expired = _open().transition(
        _cancel_event(safety=SafetyFacts(False, True, True))
    )
    retried = expired.transition(_cancel_event(at=1_500))
    assert retried is expired
    assert retried.stable_hash == expired.stable_hash
    assert retried.reason is RepriceReason.OWNERSHIP_FAILED


def test_default_has_no_live_authority_even_when_intent_is_allowed() -> None:
    for snapshot in _happy_path():
        assert snapshot.live_authority is False
        assert snapshot.permits_order_mutation is False
        for method_name in (
            "create_order",
            "place_order",
            "cancel_order",
            "replace_order",
            "save",
            "commit",
        ):
            assert not hasattr(snapshot, method_name)


def test_hashes_and_reasons_are_stable_and_decimal_canonical() -> None:
    first = _happy_path()[-1]
    equivalent = OneStepRepriceContract.open(
        opportunity_id="opp-1",
        incumbent_order_id="order-1",
        opened_at_ms=1_000,
        ttl_deadline_ms=2_000,
        frozen_menu=FrozenPriceMenu(
            side=OrderSide.BUY,
            e2=Decimal("99.9800"),
            e1=Decimal("99.990"),
            e0=Decimal("100.000"),
        ),
    )
    equivalent = equivalent.transition(_cancel_event())
    equivalent = equivalent.transition(_confirm_event())
    equivalent = equivalent.transition(
        _replace_event(best_bid="99.9500", best_ask="100.0100")
    )

    assert first.policy_hash == ONE_STEP_REPRICE_POLICY_HASH
    assert equivalent.policy_hash == ONE_STEP_REPRICE_POLICY_HASH
    assert len(first.policy_hash) == 64
    assert len(first.stable_hash) == 64
    assert first.stable_hash == equivalent.stable_hash
    assert first.reason_code == "REPLACE_INTENT_ALLOWED"
    assert first.reason is RepriceReason.REPLACE_INTENT_ALLOWED

    different_identity = OneStepRepriceContract.open(
        opportunity_id="opp-2",
        incumbent_order_id="order-1",
        opened_at_ms=1_000,
        ttl_deadline_ms=2_000,
        frozen_menu=_menu(),
    )
    assert different_identity.policy_hash == first.policy_hash
    assert different_identity.stable_hash != first.stable_hash


def test_policy_and_snapshot_hashes_ignore_ambient_decimal_context() -> None:
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_DOWN
        low_precision = _happy_path()[-1]

    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_UP
        high_precision = _happy_path()[-1]

    assert ONE_STEP_REPRICE_POLICY_HASH == (
        "09e3f19e45637b0822ab86f3c5ae875a"
        "ebb00c1580a2cbd7aec1234fe8328e0a"
    )
    assert low_precision.policy_hash == high_precision.policy_hash
    assert low_precision.stable_hash == high_precision.stable_hash


def test_frozen_menu_rejects_non_closer_or_collapsed_prices() -> None:
    with pytest.raises(RepriceContractError):
        FrozenPriceMenu(
            side=OrderSide.BUY,
            e2=Decimal("100"),
            e1=Decimal("99"),
            e0=Decimal("98"),
        )
    with pytest.raises(RepriceContractError):
        FrozenPriceMenu(
            side=OrderSide.SELL,
            e2=Decimal("100.02"),
            e1=Decimal("100.02"),
            e0=Decimal("100.00"),
        )
