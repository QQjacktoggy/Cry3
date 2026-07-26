from dataclasses import FrozenInstanceError, fields, replace
import json

import pytest

from src.gridbot.strategy.live_next.contracts import (
    ContractError,
    Decision,
    DecisionAction,
    Opportunity,
    Outcome,
    OutcomeStatus,
    canonical_dict,
    canonical_json,
    canonical_sha256,
)


def _opportunity(**overrides):
    values = {
        "session_id": "lns_1",
        "observed_at_ms": 1_000,
        "market_data_max_event_ms": 990,
        "symbol": "ETHUSDC",
        "side": "LONG",
        "expert_family": "impulse_retest",
        "anchor_event_id": "aggtrade_42",
        "regime": "TREND",
        "regime_version": "regime_v1",
        "cooldown_bucket": 4,
        "features": {"z": 2.0, "a": {"velocity": 1.5}},
        "config_hash": "cfg_abc",
    }
    values.update(overrides)
    return Opportunity.create(**values)


def _decision(opportunity=None, **overrides):
    values = {
        "decided_at_ms": 1_005,
        "action": "ACCEPT",
        "reason": "score_passed",
        "score": 74.0,
        "threshold": 70.0,
        "policy_version": "selector_v1",
        "expert_id": "impulse_retest_v1",
        "execution_profile_id": "maker_8s",
        "exit_profile_id": "tp_sl_t1t2_v1",
    }
    values.update(overrides)
    return Decision.create(opportunity or _opportunity(), **values)


def test_opportunity_is_stable_deduplicated_and_canonical():
    left = _opportunity(features={"z": 2.0, "a": {"velocity": 1.5}})
    right = _opportunity(features={"a": {"velocity": 1.5}, "z": 2.0})

    assert left.opportunity_id == right.opportunity_id
    assert left.feature_hash == right.feature_hash
    assert left.feature_payload_json == '{"a":{"velocity":1.5},"z":2.0}'
    assert left.to_dict()["features"] == {"a": {"velocity": 1.5}, "z": 2.0}
    with pytest.raises(FrozenInstanceError):
        left.symbol = "BTCUSDC"


def test_opportunity_rejects_future_market_data():
    with pytest.raises(ContractError, match="market data cannot arrive after"):
        _opportunity(market_data_max_event_ms=1_001)


def test_decision_is_structurally_outcome_blind_and_causal():
    forbidden_outcome_fields = {
        "outcome_at_ms",
        "status",
        "filled",
        "entry_filled_at_ms",
        "closed_at_ms",
        "entry_price",
        "exit_price",
        "quantity",
        "exit_reason",
        "gross_pnl_usdc",
        "all_in_cost_usdc",
        "net_pnl_usdc",
        "mfe_bp",
        "mae_bp",
    }
    decision_fields = {item.name.lower() for item in fields(Decision)}

    assert decision_fields.isdisjoint(forbidden_outcome_fields)
    assert "exit_profile_id" in decision_fields
    decision = _decision()
    assert decision.action is DecisionAction.ACCEPT
    assert decision.decision_id == Decision.build_id(
        opportunity_id=decision.opportunity_id,
        policy_version=decision.policy_version,
        config_hash=decision.config_hash,
    )
    with pytest.raises(ContractError, match="cannot precede"):
        _decision(decided_at_ms=999)


def test_accepted_decision_requires_profiles_and_threshold():
    with pytest.raises(ContractError, match="execution_profile_id"):
        _decision(execution_profile_id=None)
    with pytest.raises(ContractError, match="below threshold"):
        _decision(score=69.0)


def test_closed_outcome_reconciles_cost_after_pnl():
    outcome = Outcome.create(
        _decision(),
        outcome_at_ms=1_500,
        status="CLOSED",
        filled=True,
        entry_filled_at_ms=1_100,
        closed_at_ms=1_500,
        entry_price=2_500.0,
        exit_price=2_504.0,
        quantity=0.02,
        exit_reason="TP",
        gross_pnl_usdc=0.08,
        all_in_cost_usdc=0.02,
        net_pnl_usdc=0.06,
    )

    assert outcome.status is OutcomeStatus.CLOSED
    assert outcome.is_win is True
    assert outcome.to_dict()["net_pnl_usdc"] == pytest.approx(0.06)

    with pytest.raises(ContractError, match="gross minus all-in cost"):
        Outcome.create(
            _decision(),
            outcome_at_ms=1_500,
            status="CLOSED",
            filled=True,
            entry_filled_at_ms=1_100,
            closed_at_ms=1_500,
            entry_price=2_500.0,
            exit_price=2_504.0,
            quantity=0.02,
            exit_reason="TP",
            gross_pnl_usdc=0.08,
            all_in_cost_usdc=0.02,
            net_pnl_usdc=0.05,
        )


def test_unfilled_outcome_cannot_smuggle_fill_or_pnl_fields():
    skipped = Outcome.create(
        _decision(action="SKIP", score=65.0, execution_profile_id=None, exit_profile_id=None),
        outcome_at_ms=1_010,
        status="SKIPPED",
    )
    assert skipped.filled is False
    assert skipped.net_pnl_usdc == 0.0

    with pytest.raises(ContractError, match="unfilled outcome"):
        Outcome.create(
            _decision(action="SKIP", score=65.0, execution_profile_id=None, exit_profile_id=None),
            outcome_at_ms=1_010,
            status="SKIPPED",
            entry_price=2_500.0,
        )


def test_canonical_helpers_are_order_independent_and_reject_nan():
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert canonical_sha256({"a": 1, "b": 2}) == canonical_sha256({"b": 2, "a": 1})
    with pytest.raises(ContractError, match="non-finite"):
        canonical_json({"bad": float("nan")})


def test_canonical_dict_is_json_safe_and_rejects_key_collisions():
    decision = _decision()
    payload = canonical_dict(decision)
    assert payload == decision.to_dict()
    assert json.loads(canonical_json(decision)) == payload
    assert canonical_sha256(decision) == canonical_sha256(payload)
    with pytest.raises(ContractError, match="keys must be strings"):
        canonical_dict({1: "ambiguous", "1": "collision"})


def test_decision_link_and_schema_are_fail_closed():
    opportunity = _opportunity()
    decision = _decision(opportunity)
    decision.validate_opportunity(opportunity)
    tampered = replace(decision, feature_hash="tampered")
    with pytest.raises(ContractError, match="feature_hash"):
        tampered.validate_opportunity(opportunity)
    with pytest.raises(ContractError, match="schema_version"):
        replace(opportunity, schema_version="live_next.opportunity.v999")


def test_outcome_is_standalone_auditable_and_bound_to_decision():
    decision = _decision()
    outcome = Outcome.create(decision, outcome_at_ms=1_100, status="ENTRY_EXPIRED")
    assert outcome.observed_at_ms <= outcome.decided_at_ms <= outcome.outcome_at_ms
    assert outcome.feature_hash == decision.feature_hash
    assert outcome.terminal_outcome is OutcomeStatus.ENTRY_EXPIRED
    outcome.validate_decision(decision)
    assert canonical_dict(outcome) == outcome.to_dict()
    tampered = replace(outcome, config_hash="tampered")
    with pytest.raises(ContractError, match="config_hash"):
        tampered.validate_decision(decision)


def test_outcome_rejects_future_fill_and_action_status_mismatch():
    decision = _decision()
    expired = Outcome.create(
        decision,
        outcome_at_ms=1_200,
        status="ENTRY_EXPIRED",
    )
    with pytest.raises(ContractError, match="cannot follow outcome"):
        replace(expired, entry_filled_at_ms=1_201)
    with pytest.raises(ContractError, match="accepted decision"):
        Outcome.create(decision, outcome_at_ms=1_010, status="SKIPPED")
    skipped = _decision(
        action="SKIP",
        score=65.0,
        execution_profile_id=None,
        exit_profile_id=None,
    )
    with pytest.raises(ContractError, match="do not match"):
        Outcome.create(skipped, outcome_at_ms=1_010, status="BLOCKED")
