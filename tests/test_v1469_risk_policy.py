from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.gridbot.mainnet.v1469_risk_policy import (
    DEFAULT_RISK_POLICY,
    RISK_SNAPSHOT_MAX_AGE_MS,
    DailyRiskEvent,
    NotionalCapRequest,
    RiskAction,
    RiskDecision,
    RiskStage,
    active_day_key,
    evaluate_notional_cap,
    reduce_daily_risk,
)


BASE_MS = int(datetime(2026, 7, 25, 2, tzinfo=timezone.utc).timestamp() * 1000)


def _event(
    event_id: str,
    delta: float,
    *,
    occurred_at_ms: int = BASE_MS,
    policy_hash: str | None = None,
) -> DailyRiskEvent:
    return DailyRiskEvent(
        event_id=event_id,
        occurred_at_ms=occurred_at_ms,
        fee_net_pnl_delta_usdc=delta,
        risk_policy_hash=policy_hash or DEFAULT_RISK_POLICY.policy_hash,
    )


def _snapshot(*events: DailyRiskEvent, as_of_ms: int = BASE_MS):
    return reduce_daily_risk(
        events,
        as_of_ms=as_of_ms,
        expected_risk_policy_hash=DEFAULT_RISK_POLICY.policy_hash,
    )


def _request(**overrides) -> NotionalCapRequest:
    values = {
        "stage": RiskStage.LIVE,
        "global_cap_usdc": 50.0,
        "lane_cap_usdc": 50.0,
        "remaining_daily_risk_usdc": 0.30,
        "sl_bp": 40.0,
        "roundtrip_fee_bp": 10.0,
        "slippage_bp": 10.0,
        "exchange_min_notional_usdc": 5.0,
        "now_ms": BASE_MS,
        "expected_risk_policy_hash": DEFAULT_RISK_POLICY.policy_hash,
    }
    values.update(overrides)
    return NotionalCapRequest(**values)


def test_policy_identity_is_deterministic_and_change_sensitive() -> None:
    same = replace(DEFAULT_RISK_POLICY)
    changed = replace(DEFAULT_RISK_POLICY, cooldown_s=301)

    assert len(DEFAULT_RISK_POLICY.policy_hash) == 64
    assert same.policy_hash == DEFAULT_RISK_POLICY.policy_hash
    assert changed.policy_hash != DEFAULT_RISK_POLICY.policy_hash


@pytest.mark.parametrize(
    ("stage", "expected_cap"),
    [
        (RiskStage.SHADOW, 0.0),
        (RiskStage.PROBATION, 25.0),
        (RiskStage.LIVE, 50.0),
    ],
)
def test_stage_caps_are_shadow_zero_probation_25_live_50(
    stage: RiskStage,
    expected_cap: float,
) -> None:
    decision = evaluate_notional_cap(
        _request(stage=stage),
        _snapshot(),
    )

    assert decision.stage_cap_usdc == expected_cap
    if stage is RiskStage.SHADOW:
        assert decision.decision is RiskDecision.BLOCK
        assert decision.reason == "shadow_stage_has_zero_paid_cap"
    else:
        assert decision.decision is RiskDecision.ALLOW
        assert decision.notional_cap_usdc == expected_cap


def test_cap_is_exact_minimum_of_stage_global_lane_and_sl_normalized_risk() -> None:
    snapshot = _snapshot()
    decision = evaluate_notional_cap(
        _request(
            global_cap_usdc=45.0,
            lane_cap_usdc=40.0,
            remaining_daily_risk_usdc=0.10,
            sl_bp=10.0,
            roundtrip_fee_bp=5.0,
            slippage_bp=5.0,
        ),
        snapshot,
    )

    # 0.10 / (20 / 10_000) = 50; lane cap is therefore the minimum.
    assert decision.allowed is True
    assert decision.risk_limited_cap_usdc == pytest.approx(50.0)
    assert decision.notional_cap_usdc == pytest.approx(40.0)


def test_below_exchange_minimum_blocks_without_lifting_upward() -> None:
    decision = evaluate_notional_cap(
        _request(
            remaining_daily_risk_usdc=0.005,
            sl_bp=100.0,
            roundtrip_fee_bp=0.0,
            slippage_bp=0.0,
            exchange_min_notional_usdc=5.01,
        ),
        _snapshot(),
    )

    assert decision.decision is RiskDecision.BLOCK
    assert decision.reason == "below_exchange_min_notional"
    assert decision.safe_computed_cap_usdc == pytest.approx(0.5)
    assert decision.notional_cap_usdc == 0.0


def test_exchange_minimum_boundary_is_allowed_at_equality() -> None:
    decision = evaluate_notional_cap(
        _request(
            remaining_daily_risk_usdc=0.005,
            sl_bp=10.0,
            roundtrip_fee_bp=0.0,
            slippage_bp=0.0,
            exchange_min_notional_usdc=5.0,
        ),
        _snapshot(),
    )

    assert decision.allowed is True
    assert decision.notional_cap_usdc == pytest.approx(5.0)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("global_cap_usdc", -1.0),
        ("lane_cap_usdc", float("nan")),
        ("remaining_daily_risk_usdc", float("inf")),
        ("sl_bp", -0.01),
        ("roundtrip_fee_bp", float("-inf")),
        ("slippage_bp", float("nan")),
        ("exchange_min_notional_usdc", -1.0),
        ("now_ms", -1),
    ],
)
def test_nonfinite_or_negative_submit_inputs_fail_closed(
    field: str,
    bad_value: float,
) -> None:
    decision = evaluate_notional_cap(
        _request(**{field: bad_value}),
        _snapshot(),
    )

    assert decision.decision is RiskDecision.BLOCK
    assert decision.notional_cap_usdc == 0.0
    assert decision.reason == "invalid_numeric_input"


def test_zero_total_sl_and_cost_fails_closed() -> None:
    decision = evaluate_notional_cap(
        _request(sl_bp=0.0, roundtrip_fee_bp=0.0, slippage_bp=0.0),
        _snapshot(),
    )

    assert decision.decision is RiskDecision.BLOCK
    assert decision.reason == "invalid_all_in_loss_bp"


def test_stale_future_and_hash_mismatched_snapshots_fail_closed() -> None:
    snapshot = _snapshot()
    stale = evaluate_notional_cap(
        _request(now_ms=BASE_MS + RISK_SNAPSHOT_MAX_AGE_MS + 1),
        snapshot,
    )
    boundary = evaluate_notional_cap(
        _request(now_ms=BASE_MS + RISK_SNAPSHOT_MAX_AGE_MS),
        snapshot,
    )
    future_snapshot = _snapshot(as_of_ms=BASE_MS + 1)
    future = evaluate_notional_cap(_request(), future_snapshot)
    mismatch = evaluate_notional_cap(
        _request(expected_risk_policy_hash="0" * 64),
        snapshot,
    )

    assert stale.reason == "stale_risk_snapshot"
    assert boundary.allowed is True
    assert future.reason == "future_risk_snapshot"
    assert mismatch.reason == "risk_policy_hash_mismatch"


@pytest.mark.parametrize(
    ("snapshot_change", "expected_reason"),
    [
        ({"risk_policy_hash": "f" * 64}, "risk_policy_hash_mismatch"),
        ({"remaining_daily_risk_usdc": -0.01}, "invalid_risk_snapshot"),
        ({"high_water_usdc": float("nan")}, "invalid_risk_snapshot"),
    ],
)
def test_tampered_snapshot_fails_closed(
    snapshot_change: dict[str, object],
    expected_reason: str,
) -> None:
    tampered = replace(_snapshot(), **snapshot_change)
    decision = evaluate_notional_cap(_request(), tampered)

    assert decision.decision is RiskDecision.BLOCK
    assert decision.reason == expected_reason


@pytest.mark.parametrize(
    "bad_as_of_ms",
    [-1, float("nan"), float("inf")],
)
def test_invalid_reducer_clock_returns_fail_closed_snapshot(
    bad_as_of_ms: float,
) -> None:
    snapshot = reduce_daily_risk([], as_of_ms=bad_as_of_ms)  # type: ignore[arg-type]

    assert snapshot.data_valid is False
    assert snapshot.entry_blocked is True
    assert snapshot.reason == "invalid_as_of_ms"


def test_future_or_nonfinite_paid_close_event_fails_closed() -> None:
    future = _snapshot(
        _event("future", 0.10, occurred_at_ms=BASE_MS + 1),
        as_of_ms=BASE_MS,
    )
    nonfinite = _snapshot(_event("nan-pnl", float("nan")))

    assert future.reason == "future_event"
    assert nonfinite.reason == "invalid_event"
    assert future.entry_blocked is True
    assert nonfinite.entry_blocked is True


def test_sl_and_cost_increases_can_only_reduce_or_preserve_cap() -> None:
    snapshot = _snapshot()
    caps = []
    for all_in_bp in (10.0, 20.0, 40.0, 80.0, 160.0):
        decision = evaluate_notional_cap(
            _request(
                sl_bp=all_in_bp / 2,
                roundtrip_fee_bp=all_in_bp / 4,
                slippage_bp=all_in_bp / 4,
            ),
            snapshot,
        )
        caps.append(decision.safe_computed_cap_usdc)

    assert caps == sorted(caps, reverse=True)


def test_soft_loss_boundary_latches_and_caps_new_entries_at_25() -> None:
    snapshot = _snapshot(_event("loss", -0.15))
    decision = evaluate_notional_cap(_request(), snapshot)

    assert snapshot.soft_loss_triggered is True
    assert snapshot.hard_loss_triggered is False
    assert snapshot.entry_blocked is False
    assert decision.allowed is True
    assert decision.reason == "daily_soft_loss_cap"
    assert decision.notional_cap_usdc <= 25.0


def test_hard_loss_boundary_latches_and_stops_new_paid_entries() -> None:
    snapshot = _snapshot(
        _event("loss", -0.30),
        _event("later_recovery", 0.50, occurred_at_ms=BASE_MS + 1),
        as_of_ms=BASE_MS + 1,
    )
    decision = evaluate_notional_cap(
        _request(now_ms=BASE_MS + 1),
        snapshot,
    )

    assert snapshot.closed_fee_net_pnl_usdc == pytest.approx(0.20)
    assert snapshot.hard_loss_triggered is True
    assert snapshot.entry_blocked is True
    assert decision.decision is RiskDecision.BLOCK
    assert decision.reason == "daily_hard_loss"


def test_positive_high_water_floor_is_dynamic_and_latches_after_giveback() -> None:
    snapshot = _snapshot(
        _event("win", 0.30),
        _event("giveback", -0.15, occurred_at_ms=BASE_MS + 1),
        _event("recovery", 0.20, occurred_at_ms=BASE_MS + 2),
        as_of_ms=BASE_MS + 2,
    )

    assert snapshot.high_water_usdc == pytest.approx(0.35)
    assert snapshot.profit_floor_usdc == pytest.approx(0.20)
    assert snapshot.profit_floor_triggered is True
    assert snapshot.entry_blocked is True
    assert evaluate_notional_cap(
        _request(now_ms=BASE_MS + 2),
        snapshot,
    ).reason == "daily_profit_floor"


def test_taipei_day_rollover_resets_prior_day_events() -> None:
    before_rollover = int(
        datetime(2026, 7, 24, 15, 59, 59, tzinfo=timezone.utc).timestamp()
        * 1000
    )
    after_rollover = int(
        datetime(2026, 7, 24, 16, 0, 0, tzinfo=timezone.utc).timestamp()
        * 1000
    )

    assert active_day_key(before_rollover) == "2026-07-24"
    assert active_day_key(after_rollover) == "2026-07-25"

    snapshot = reduce_daily_risk(
        [_event("prior_day_loss", -0.30, occurred_at_ms=before_rollover)],
        as_of_ms=after_rollover,
    )
    assert snapshot.active_day == "2026-07-25"
    assert snapshot.closed_fee_net_pnl_usdc == 0.0
    assert snapshot.paid_closed_event_count == 0
    assert snapshot.entry_blocked is False


def test_restart_reducer_is_deterministic_and_exact_duplicates_are_idempotent() -> None:
    first = _event("a", 0.10, occurred_at_ms=BASE_MS)
    second = _event("b", -0.03, occurred_at_ms=BASE_MS + 1)

    before_restart = _snapshot(first, second, as_of_ms=BASE_MS + 1)
    after_restart = _snapshot(
        second,
        first,
        replace(first),
        as_of_ms=BASE_MS + 1,
    )

    assert after_restart == before_restart
    assert before_restart.event_ids == ("a", "b")
    assert len(before_restart.evidence_revision) == 64


def test_conflicting_duplicate_event_fails_closed() -> None:
    first = _event("same", 0.10)
    conflicting = _event("same", 0.11)

    snapshot = _snapshot(first, conflicting)

    assert snapshot.data_valid is False
    assert snapshot.entry_blocked is True
    assert snapshot.reason == "conflicting_duplicate_event"


def test_policy_mismatched_event_fails_closed() -> None:
    snapshot = _snapshot(_event("wrong", 0.10, policy_hash="f" * 64))

    assert snapshot.data_valid is False
    assert snapshot.reason == "risk_policy_hash_mismatch"


def test_active_position_risk_reducing_exit_is_always_allowed() -> None:
    stale_blocked_snapshot = _snapshot(
        _event("hard_loss", -0.30),
        as_of_ms=BASE_MS,
    )
    exit_decision = evaluate_notional_cap(
        _request(
            action=RiskAction.RISK_REDUCING_EXIT,
            active_position=True,
            now_ms=BASE_MS + 999_999,
            expected_risk_policy_hash="mismatch",
            global_cap_usdc=float("nan"),
        ),
        stale_blocked_snapshot,
    )
    no_position = evaluate_notional_cap(
        _request(
            action=RiskAction.RISK_REDUCING_EXIT,
            active_position=False,
        ),
        _snapshot(),
    )

    assert exit_decision.allowed is True
    assert exit_decision.reason == "risk_reducing_exit_always_allowed"
    assert no_position.decision is RiskDecision.BLOCK
    assert no_position.reason == "risk_reducing_exit_requires_active_position"
