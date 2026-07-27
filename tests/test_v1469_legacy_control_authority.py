from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.gridbot.mainnet.v1469_adaptive_identity import (
    BreakevenPolicy, DcaPolicy, EarlyFailPolicy, MarketStateIdentity,
    RepricePolicy, RunnerPolicy, TakeProfitLevel, TrailPolicy,
)
from src.gridbot.mainnet.v1469_arm_arbiter import (
    ArbiterDecision, ArmIdentity, LeaseAction, LeasePhase, LeaseProposal,
)
from src.gridbot.mainnet.v1469_legacy_control import LegacyExecutionSnapshot
from src.gridbot.mainnet.v1469_paid_authority import (
    LegacyPaidDecision, PaidAuthorityKind, PaidProbationEvidence,
    apply_automatic_live_phase, resolve_paid_authority,
)
from src.gridbot.mainnet.v1469_paired_evaluator import (
    AggTradePathTick, MatchedArmOpportunity, ShadowCostModel, TickEnvelope,
    evaluate_paired_arms,
)


def snapshot(**changes):
    values = dict(market_identity=MarketStateIdentity(environment="MAINNET", symbol="ETHUSDC",
        lane_code="W6A", effective_side="LONG", strategy="S1_BB_RSI",
        coarse_regime="RANGE", market_state="range"), entry_offset_bp=1.0,
        entry_type="LIMIT", entry_ttl_s=90, maker_mode="POST_ONLY",
        take_profits=(TakeProfitLevel(level_id="FULL", target_bp=8, fraction=1),),
        sl_bp=8, max_hold_s=360, reprice=RepricePolicy(),
        breakeven=BreakevenPolicy(), trail=TrailPolicy(), runner=RunnerPolicy(),
        early_fail=EarlyFailPolicy(), dca=DcaPolicy(), lane_notional_cap_usdc=25,
        global_notional_cap_usdc=50, risk_policy_hash="risk-a", reference_price=2000)
    values.update(changes)
    return LegacyExecutionSnapshot(**values)


def test_exact_profile_hash_excludes_submit_price_and_caps():
    base = snapshot()
    assert base.execution_profile.profile_id == "LEGACY_CONTROL"
    assert replace(base, reference_price=2100).execution_profile.profile_hash == base.execution_profile.profile_hash
    assert replace(base, lane_notional_cap_usdc=20).execution_profile.profile_hash == base.execution_profile.profile_hash
    assert replace(base, sl_bp=9).execution_profile.profile_hash != base.execution_profile.profile_hash
    assert replace(base, take_profits=(TakeProfitLevel(level_id="FULL", target_bp=9, fraction=1),)).execution_profile.profile_hash != base.execution_profile.profile_hash
    assert replace(base, entry_ttl_s=91).execution_profile.profile_hash != base.execution_profile.profile_hash
    payload = base.to_payload()
    assert payload["profile_id"] == "LEGACY_CONTROL"
    assert payload["execution_profile_hash"] == base.execution_profile.profile_hash
    assert payload["submit_authority_hash"] == base.submit_authority_hash
    assert payload["take_profits"] == [
        {"level_id": "FULL", "target_bp": 8.0, "fraction": 1.0}
    ]
    # Submit-only cap/reference inputs remain observable and change the
    # authority hash without fragmenting the execution cohort.
    assert replace(base, lane_notional_cap_usdc=20).submit_authority_hash != base.submit_authority_hash
    assert replace(base, reference_price=2100).submit_authority_hash != base.submit_authority_hash

    with pytest.raises(ValueError, match="must be positive"):
        snapshot(reference_price=float("nan"))
    with pytest.raises(ValueError, match="must cover"):
        snapshot(lane_notional_cap_usdc=60, global_notional_cap_usdc=50)


def test_legacy_snapshot_strict_round_trip_and_corruption_fails_closed():
    snap = snapshot()
    assert LegacyExecutionSnapshot.from_payload(snap.to_payload()) == snap
    corrupt = snap.to_payload()
    corrupt["execution_profile_hash"] = "0" * 64
    with pytest.raises(ValueError, match="profile hash mismatch"):
        LegacyExecutionSnapshot.from_payload(corrupt)
    extra = {**snap.to_payload(), "future_execution_control": True}
    with pytest.raises(ValueError, match="schema mismatch"):
        LegacyExecutionSnapshot.from_payload(extra)


def test_take_profit_fractions_must_be_exact_and_finite():
    with pytest.raises(ValueError, match="sum exactly"):
        snapshot(take_profits=(
            TakeProfitLevel(level_id="A", target_bp=5, fraction=.4),
            TakeProfitLevel(level_id="B", target_bp=9, fraction=.59),
        ))


def _decision(snap):
    arm = ArmIdentity(arm_key="adaptive-a", lane_code="W6A", side="LONG",
        strategy="S1_BB_RSI", regime="RANGE", execution_profile_id="RANGE_SCALP",
        execution_profile_hash="adaptive-hash")
    return ArbiterDecision(winner=arm, blockers=(), evaluations=(), evidence_revision="rev-2",
        lease_proposal=LeaseProposal(action=LeaseAction.KEEP, arm_key=arm.arm_key,
            phase=LeasePhase.PROBATION, evidence_revision="rev-2", expires_at_ms=20000), revocations=())


def test_probation_auto_live_and_enforcement_off_equivalence():
    snap = snapshot()
    decision = _decision(snap)
    evidence = PaidProbationEvidence(arm_key="adaptive-a", execution_profile_hash="adaptive-hash",
        evidence_revision="rev-2", regime="RANGE", as_of_ms=9999,
        terminal_fills=3, wins=2, fee_net_paid_pnl=.01)
    promoted = apply_automatic_live_phase(decision, evidence, now_ms=10000)
    assert promoted.lease_proposal.phase is LeasePhase.LIVE
    assert promoted.lease_proposal.expires_at_ms == 610000
    legacy = LegacyPaidDecision(allowed=True, decision_payload={"legacy": "unchanged"}, reason="allowed")
    result = resolve_paid_authority(legacy_snapshot=snap, legacy_decision=legacy,
        arbiter_decision=decision, durable_lease=object(), probation_evidence=evidence,
        daily_risk_snapshot=None, enforcement_enabled=False, now_ms=10000, regime="RANGE")
    assert result.kind is PaidAuthorityKind.LEGACY_CONTROL
    assert result.legacy_decision is legacy
    assert result.lease_id is None


def test_enforcement_on_requires_exact_fresh_identity_and_risk():
    snap = snapshot()
    decision = _decision(snap)
    evidence = PaidProbationEvidence(arm_key="adaptive-a", execution_profile_hash="adaptive-hash",
        evidence_revision="rev-2", regime="RANGE", as_of_ms=9999,
        terminal_fills=3, wins=2, fee_net_paid_pnl=1)
    lease = SimpleNamespace(status="ACTIVE", expires_at_ms=20000, arm_key="adaptive-a",
        coarse_regime="RANGE", execution_profile_hash="adaptive-hash", evidence_revision="rev-2",
        phase=LeasePhase.LIVE, lease_id="lease-a", risk_policy_hash="risk-a", notional_cap_usdc=20)
    risk = SimpleNamespace(data_valid=True, entry_blocked=False, as_of_ms=9999)
    kwargs=dict(legacy_snapshot=snap, legacy_decision=LegacyPaidDecision(allowed=True,
        decision_payload={}, reason="ok"), arbiter_decision=decision, durable_lease=lease,
        probation_evidence=evidence, daily_risk_snapshot=risk, enforcement_enabled=True,
        now_ms=10000, regime="RANGE")
    assert resolve_paid_authority(**kwargs).kind is PaidAuthorityKind.ADAPTIVE
    invalid = resolve_paid_authority(
        **{**kwargs, "durable_lease": replace(evidence, arm_key="wrong")}
    )
    assert invalid.kind is PaidAuthorityKind.BLOCK
    assert invalid.reason == "adaptive_authority_invalid_block"
    assert invalid.execution_payload is None
    blocked = resolve_paid_authority(**{**kwargs, "legacy_decision": LegacyPaidDecision(allowed=False, decision_payload=None, reason="risk"), "daily_risk_snapshot": None})
    assert blocked.kind is PaidAuthorityKind.BLOCK


def test_ordinary_sl_does_not_become_hard_loss():
    evidence = PaidProbationEvidence(arm_key="a", execution_profile_hash="h",
        evidence_revision="r", regime="RANGE", as_of_ms=1, terminal_fills=3,
        wins=2, fee_net_paid_pnl=1, hard_loss_marker=False)
    assert evidence.live_ready
    assert not replace(evidence, hard_loss_marker=True).live_ready


@pytest.mark.parametrize("regime", ["RANGE", "TREND_UP", "TREND_DOWN"])
def test_legacy_control_is_in_same_paired_envelope(regime):
    snap = snapshot(market_identity=replace(snapshot().market_identity,
        coarse_regime=regime, market_state=regime.lower()))
    opportunity = MatchedArmOpportunity(opportunity_id="opp-a", candidate_status="SAFE",
        market_identity=snap.market_identity, signal_price=2000,
        legacy_profile=snap.profile_definition)
    envelope = TickEnvelope(opportunity_id="opp-a", observed_at_ms=1000,
        decision_at_ms=800000, coverage_through_ms=800000,
        ticks=(AggTradePathTick(timestamp_ms=2000, available_at_ms=2000,
            aggregate_trade_id=1, price=1999),), provenance="fixture")
    result = evaluate_paired_arms(opportunity, envelope,
        ShadowCostModel(maker_fee_bp=1, taker_fee_bp=2,
            adverse_slippage_bp=0, provenance="fixture"))
    legacy = next(item for item in result.results if item.profile_id == "LEGACY_CONTROL")
    adaptive = [item for item in result.results if item.profile_id not in {"LEGACY_CONTROL", "RISK_OFF"}]
    assert adaptive
    assert all(item.envelope_hash == legacy.envelope_hash for item in adaptive)
    assert all(item.opportunity_id == legacy.opportunity_id for item in adaptive)
