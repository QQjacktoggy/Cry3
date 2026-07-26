from __future__ import annotations

from dataclasses import replace

import pytest

from src.gridbot.mainnet.v1469_arm_arbiter import (
    ArbiterDecision,
    ArmIdentity,
    LeaseAction,
    LeasePhase,
    LeaseProposal,
)
from src.gridbot.mainnet.v1469_arm_profiles import (
    PASSIVE_BALANCED,
    TREND_PARTIAL,
    get_arm_profile,
)
from src.gridbot.mainnet.v1469_authority_runtime import (
    AuthorityRuntimeResult,
    CurrentOpportunityEligibility,
)
from src.gridbot.mainnet.v1469_execution_plan import (
    V1469PaidExecutionPlan,
    apply_paid_execution_plan,
    build_paid_execution_plan,
)
from src.gridbot.storage.v1469_arm_observation_repository import arm_identity
from src.gridbot.storage.v1469_lease_repository import DurableArmLease
from src.gridbot.strategy.long_pullback import SignalPlan
from src.gridbot.strategy.wildcat_live import WildcatLiveDecision


def _authority(
    profile_id: str = TREND_PARTIAL,
    *,
    phase: LeasePhase = LeasePhase.PROBATION,
) -> AuthorityRuntimeResult:
    profile = get_arm_profile(profile_id)
    profile_hash = str(profile.execution_profile_hash)
    arm_key = arm_identity(
        {
            "lane_code": "W6A",
            "effective_side": "LONG",
            "strategy": "W6A",
            "coarse_regime": "TREND_UP",
            "execution_profile_id": profile_id,
            "execution_profile_schema": "v1469.execution-profile.1",
            "execution_profile_hash": profile_hash,
        }
    )
    winner = ArmIdentity(
        arm_key=arm_key,
        lane_code="W6A",
        side="LONG",
        strategy="W6A",
        regime="TREND_UP",
        execution_profile_id=profile_id,
        execution_profile_hash=profile_hash,
    )
    decision = ArbiterDecision(
        winner=winner,
        blockers=(),
        evaluations=(),
        evidence_revision="revision-1",
        lease_proposal=LeaseProposal(
            action=LeaseAction.KEEP,
            arm_key=arm_key,
            phase=phase,
            evidence_revision="revision-1",
            expires_at_ms=20_000,
        ),
        revocations=(),
    )
    lease = DurableArmLease(
        arm_key=arm_key,
        lease_id="lease-1",
        generation=3,
        environment="MAINNET",
        symbol="BTCUSDC",
        lane_code="W6A",
        effective_side="LONG",
        strategy="W6A",
        coarse_regime="TREND_UP",
        execution_profile_id=profile_id,
        execution_profile_schema="v1469.execution-profile.1",
        execution_profile_hash=profile_hash,
        phase=phase,
        status="ACTIVE",
        notional_cap_usdc=25.0 if phase is LeasePhase.PROBATION else 50.0,
        risk_policy_hash="a" * 64,
        evidence_revision="revision-1",
        evidence_as_of_ms=9_000,
        issued_at_ms=8_000,
        renewed_at_ms=9_000,
        expires_at_ms=20_000,
        owner_id="owner",
        boot_id="boot",
        demotion_reason=None,
        demoted_at_ms=None,
        cooldown_until_ms=None,
        created_at_ms=8_000,
        updated_at_ms=9_000,
    )
    current = CurrentOpportunityEligibility(
        opportunity_id="opportunity-1",
        candidate_id="candidate-1",
        observed_at_ms=9_900,
        arm_key=arm_key,
        lane_code="W6A",
        side="LONG",
        strategy="W6A",
        regime="TREND_UP",
        execution_profile_id=profile_id,
        execution_profile_hash=profile_hash,
    )
    return AuthorityRuntimeResult(
        submit_admissible=True,
        blockers=(),
        decision=decision,
        arbiter_request=None,
        evidence_mapping=None,
        current_opportunity=current,
        durable_lease=lease,
        lease_mutation=None,
        ledger_row_count=10,
        ledger_scope_complete=True,
        ledger_revision="ledger-1",
    )


def _decision(side: str = "LONG") -> WildcatLiveDecision:
    return WildcatLiveDecision(
        signal=SignalPlan(
            action="BUY" if side == "LONG" else "SELL",
            confidence=80,
            score=5,
            symbol="BTCUSDC",
            price=100.0,
            rsi=None,
            atr=None,
            support=None,
            vwap=None,
            planned_notional_usdc=50.0,
        ),
        strategy="legacy",
        side=side,
        tp_pct=0.01,
        sl_pct=0.01,
        partial_exit_pct=0.5,
        partial_tp_pct=0.005,
        recovery_steps=1,
        recovery_trigger_pct=0.01,
        recovery_tp_shrink=0.5,
        adverse_exit_bars=2,
        adverse_exit_loss_pct=0.01,
        max_holding_bars=30,
        params_label="legacy",
    )


def test_build_and_apply_trend_partial_plan_is_exact() -> None:
    authority = _authority()
    plan = build_paid_execution_plan(
        authority,
        approved_notional_usdc=25.0,
    )
    assert plan.execution_profile_id == TREND_PARTIAL
    assert [item.target_bp for item in plan.take_profits] == [6.0, 16.0]
    assert [item.fraction for item in plan.take_profits] == [0.70, 0.30]
    assert plan.entry_ttl_s == 60
    assert plan.sl_bp == 10.0

    applied = apply_paid_execution_plan(
        _decision(),
        plan,
        reference_price=100.0,
        leverage=10,
    )
    assert applied.strategy == "W6A"
    assert applied.recovery_steps == 0
    assert applied.adverse_exit_bars == 0
    assert applied.max_holding_bars == 12
    assert applied.signal.planned_notional_usdc == 25.0
    assert applied.signal.planned_margin_usdc == 2.5
    assert applied.signal.entries == pytest.approx([99.98])
    assert applied.signal.stop_loss == pytest.approx(99.88002)
    assert applied.signal.take_profits == pytest.approx(
        [100.039988, 100.139968]
    )


def test_plan_freeze_rejects_identity_and_phase_cap_changes() -> None:
    authority = _authority()
    assert authority.durable_lease is not None
    with pytest.raises(ValueError, match="identity changed"):
        build_paid_execution_plan(
            replace(
                authority,
                durable_lease=replace(
                    authority.durable_lease,
                    evidence_revision="new-revision",
                ),
            ),
            approved_notional_usdc=25.0,
        )
    with pytest.raises(ValueError, match="cap"):
        build_paid_execution_plan(
            authority,
            approved_notional_usdc=25.01,
        )


def test_full_tp_profile_does_not_create_a_fake_partial_tail() -> None:
    plan = build_paid_execution_plan(
        _authority(PASSIVE_BALANCED),
        approved_notional_usdc=20.0,
    )
    applied = apply_paid_execution_plan(
        _decision(),
        plan,
        reference_price=100.0,
        leverage=10,
    )
    assert len(plan.take_profits) == 1
    assert applied.partial_exit_pct == 1.0
    assert applied.partial_tp_pct == applied.tp_pct
    assert applied.signal.take_profits == pytest.approx([100.059984])


def test_plan_rejects_wrong_side_and_non_admissible_authority() -> None:
    authority = _authority()
    plan = build_paid_execution_plan(
        authority,
        approved_notional_usdc=25.0,
    )
    with pytest.raises(ValueError, match="side"):
        apply_paid_execution_plan(
            _decision("SHORT"),
            plan,
            reference_price=100.0,
            leverage=10,
        )
    with pytest.raises(ValueError, match="not submit-admissible"):
        build_paid_execution_plan(
            replace(authority, submit_admissible=False),
            approved_notional_usdc=25.0,
        )


def test_execution_plan_payload_contains_exact_profile_geometry() -> None:
    plan = build_paid_execution_plan(
        _authority(),
        approved_notional_usdc=25.0,
    )
    payload = plan.to_payload()
    assert payload["schema"] == "v1469.paid-execution-plan.1"
    assert payload["lease_generation"] == 3
    assert payload["evidence_revision"] == "revision-1"
    assert payload["take_profits"] == [
        {"level_id": "TP1", "target_bp": 6.0, "fraction": 0.7},
        {"level_id": "FULL", "target_bp": 16.0, "fraction": 0.3},
    ]
