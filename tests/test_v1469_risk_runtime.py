from __future__ import annotations

from dataclasses import replace

import pytest

from src.gridbot.mainnet.v1469_adaptive_identity import RiskPolicy
from src.gridbot.mainnet.v1469_arm_arbiter import (
    ArbiterDecision,
    ArmIdentity,
    LeaseAction,
    LeasePhase,
    LeaseProposal,
)
from src.gridbot.mainnet.v1469_authority_runtime import AuthorityRuntimeResult
from src.gridbot.mainnet.v1469_risk_policy import DailyRiskEvent
from src.gridbot.mainnet.v1469_risk_runtime import (
    V1469RiskAdmissionRuntime,
    risk_policy_from_settings,
)
from src.gridbot.storage.v1469_lease_repository import DurableArmLease


def _policy() -> RiskPolicy:
    return RiskPolicy(
        policy_id="V1469_PHASE_C",
        paid_notional_cap_usdc=50.0,
        per_trade_loss_cap_usdc=0.30,
        lane_open_notional_cap_usdc=50.0,
        global_open_notional_cap_usdc=50.0,
        daily_soft_loss_cap_usdc=0.15,
        daily_hard_loss_cap_usdc=0.30,
        daily_profit_lock_trigger_usdc=0.15,
        daily_profit_lock_giveback_usdc=0.15,
        max_consecutive_losses=2,
        cooldown_s=300,
    )


def _authority(
    policy: RiskPolicy,
    *,
    phase: LeasePhase = LeasePhase.PROBATION,
) -> AuthorityRuntimeResult:
    identity = ArmIdentity(
        arm_key="arm-1",
        lane_code="W6A",
        side="LONG",
        strategy="W6A",
        regime="TREND_UP",
        execution_profile_id="TREND_PARTIAL",
        execution_profile_hash="profile-1",
    )
    decision = ArbiterDecision(
        winner=identity,
        blockers=(),
        evaluations=(),
        evidence_revision="revision-1",
        lease_proposal=LeaseProposal(
            action=LeaseAction.KEEP,
            arm_key="arm-1",
            phase=phase,
            evidence_revision="revision-1",
            expires_at_ms=20_000,
        ),
        revocations=(),
    )
    lease = DurableArmLease(
        arm_key="arm-1",
        lease_id="lease-1",
        generation=1,
        environment="MAINNET",
        symbol="BTCUSDC",
        lane_code="W6A",
        effective_side="LONG",
        strategy="W6A",
        coarse_regime="TREND_UP",
        execution_profile_id="TREND_PARTIAL",
        execution_profile_schema="v1469.execution-profile.1",
        execution_profile_hash="profile-1",
        phase=phase,
        status="ACTIVE",
        notional_cap_usdc=25.0 if phase is LeasePhase.PROBATION else 50.0,
        risk_policy_hash=policy.policy_hash,
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
    return AuthorityRuntimeResult(
        submit_admissible=True,
        blockers=(),
        decision=decision,
        arbiter_request=None,
        evidence_mapping=None,
        current_opportunity=None,
        durable_lease=lease,
        lease_mutation=None,
        ledger_row_count=1,
        ledger_scope_complete=True,
        ledger_revision="ledger-1",
    )


class _Repository:
    def __init__(self, events=()):
        self.events = tuple(events)
        self.calls = []

    async def load_active_day_events(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self.events


@pytest.mark.asyncio
async def test_probation_risk_admission_caps_at_25_and_reserves_loss() -> None:
    policy = _policy()
    repository = _Repository()
    result = await V1469RiskAdmissionRuntime(repository).evaluate(
        _authority(policy),
        desired_notional_usdc=50.0,
        sl_bp=10.0,
        roundtrip_fee_bp=4.0,
        slippage_bp=2.0,
        exchange_min_notional_usdc=5.0,
        policy=policy,
        now_ms=10_000,
    )
    assert result.allowed
    assert result.approved_notional_usdc == 25.0
    assert result.reserved_loss_usdc == pytest.approx(0.04)
    assert repository.calls == [
        {
            "environment": "MAINNET",
            "symbol": "BTCUSDC",
            "as_of_ms": 10_000,
            "limit": 10_000,
        }
    ]


@pytest.mark.asyncio
async def test_daily_hard_loss_blocks_new_entry() -> None:
    policy = _policy()
    repository = _Repository(
        (
            DailyRiskEvent(
                event_id="loss",
                occurred_at_ms=9_000,
                fee_net_pnl_delta_usdc=-0.30,
                risk_policy_hash=policy.policy_hash,
            ),
        )
    )
    result = await V1469RiskAdmissionRuntime(repository).evaluate(
        _authority(policy),
        desired_notional_usdc=25.0,
        sl_bp=10.0,
        roundtrip_fee_bp=4.0,
        slippage_bp=2.0,
        exchange_min_notional_usdc=5.0,
        policy=policy,
        now_ms=10_000,
    )
    assert not result.allowed
    assert result.approved_notional_usdc == 0.0
    assert result.reason == "daily_hard_loss"


@pytest.mark.asyncio
async def test_policy_hash_and_exchange_minimum_fail_closed() -> None:
    policy = _policy()
    runtime = V1469RiskAdmissionRuntime(_Repository())
    authority = _authority(policy)
    assert authority.durable_lease is not None
    with pytest.raises(ValueError, match="risk-policy hash"):
        await runtime.evaluate(
            replace(
                authority,
                durable_lease=replace(
                    authority.durable_lease,
                    risk_policy_hash="b" * 64,
                ),
            ),
            desired_notional_usdc=25.0,
            sl_bp=10.0,
            roundtrip_fee_bp=4.0,
            slippage_bp=2.0,
            exchange_min_notional_usdc=5.0,
            policy=policy,
            now_ms=10_000,
        )
    result = await runtime.evaluate(
        authority,
        desired_notional_usdc=4.0,
        sl_bp=10.0,
        roundtrip_fee_bp=4.0,
        slippage_bp=2.0,
        exchange_min_notional_usdc=5.0,
        policy=policy,
        now_ms=10_000,
    )
    assert not result.allowed
    assert result.reason == "desired_notional_below_exchange_minimum"

def test_settings_builder_uses_configured_per_trade_cap() -> None:
    class Settings:
        mainnet_codex_v1469_live_notional_usdc = 50.0
        mainnet_codex_v1469_per_trade_loss_cap_usdc = 0.15
        mainnet_codex_v1469_lane_open_notional_usdc = 50.0
        mainnet_codex_v1469_global_open_notional_usdc = 50.0
        mainnet_codex_v1469_daily_soft_loss_usdc = 0.15
        mainnet_codex_v1469_daily_hard_loss_usdc = 0.30
        mainnet_codex_v1469_daily_profit_lock_trigger_usdc = 0.15
        mainnet_codex_v1469_daily_profit_lock_giveback_usdc = 0.15

    policy = risk_policy_from_settings(Settings())
    assert policy.per_trade_loss_cap_usdc == 0.15
    assert policy.policy_hash != _policy().policy_hash