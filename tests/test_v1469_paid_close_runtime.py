from __future__ import annotations

from dataclasses import replace

import pytest

from src.gridbot.mainnet.v1469_paid_close_runtime import (
    V1469PaidCloseRuntime,
)
from src.gridbot.storage.v1469_paid_execution_claim_repository import (
    DurablePaidExecutionClaim,
    PaidClaimMutationResult,
    V1469PaidClaimConflictError,
)


def _claim(status: str = "SUBMITTED") -> DurablePaidExecutionClaim:
    return DurablePaidExecutionClaim(
        claim_id="claim-1",
        environment="MAINNET",
        symbol="BTCUSDC",
        opportunity_id="opportunity-1",
        arm_key="arm-1",
        lease_id="lease-1",
        lease_generation=3,
        evidence_revision="revision-1",
        regime="TREND_UP",
        execution_profile_hash="profile-1",
        risk_policy_hash="a" * 64,
        approved_notional_usdc=25.0,
        reserved_loss_usdc=0.04,
        status=status,
        generation=3 if status == "SUBMITTED" else 4,
        claimed_at_ms=100,
        terminal_at_ms=200 if status == "TERMINAL" else None,
        terminal_reason="TP" if status == "TERMINAL" else None,
        result_payload=(
            {
                "risk_event_id": "v1469-paid-close:claim-1",
                "fee_net_pnl_usdc": 0.02,
                "source_run_id": "run-1",
                "hard_loss_marker": False,
            }
            if status == "TERMINAL"
            else None
        ),
        created_at_ms=100,
        updated_at_ms=200 if status == "TERMINAL" else 150,
    )


class _ClaimRepository:
    def __init__(self, claim: DurablePaidExecutionClaim):
        self.claim = claim
        self.terminal_calls = []
        self.fail_terminal = False

    async def get_claim_by_id(self, claim_id: str):
        return self.claim if claim_id == self.claim.claim_id else None

    async def terminalize_claim(self, **kwargs):
        self.terminal_calls.append(dict(kwargs))
        if self.fail_terminal:
            raise V1469PaidClaimConflictError("generation changed")
        self.claim = replace(
            self.claim,
            status="TERMINAL",
            generation=self.claim.generation + 1,
            terminal_at_ms=int(kwargs["terminal_at_ms"]),
            terminal_reason=str(kwargs["terminal_reason"]),
            result_payload=dict(kwargs["result_payload"]),
            updated_at_ms=int(kwargs["terminal_at_ms"]),
        )
        return PaidClaimMutationResult(self.claim, True, False)


class _RiskRepository:
    def __init__(self):
        self.calls = []
        self.inserted_ids = set()

    async def append_event(self, event, **kwargs):
        self.calls.append((event, dict(kwargs)))
        inserted = event.event_id not in self.inserted_ids
        self.inserted_ids.add(event.event_id)
        return inserted


@pytest.mark.asyncio
async def test_close_writes_daily_risk_before_releasing_claim() -> None:
    claims = _ClaimRepository(_claim())
    risk = _RiskRepository()
    result = await V1469PaidCloseRuntime(claims, risk).record_close(
        claim_id="claim-1",
        fee_net_pnl_usdc=0.02,
        terminal_reason="TP",
        occurred_at_ms=200,
        source_run_id="run-1",
    )
    assert result.claim.status == "TERMINAL"
    assert result.risk_event_inserted
    assert len(risk.calls) == 1
    assert len(claims.terminal_calls) == 1
    event, kwargs = risk.calls[0]
    assert event.event_id == "v1469-paid-close:claim-1"
    assert event.fee_net_pnl_delta_usdc == 0.02
    assert kwargs["environment"] == "MAINNET"
    assert claims.terminal_calls[0]["result_payload"]["risk_event_id"] == (
        "v1469-paid-close:claim-1"
    )


@pytest.mark.asyncio
async def test_terminal_failure_keeps_claim_reservation_after_risk_event() -> None:
    claims = _ClaimRepository(_claim())
    claims.fail_terminal = True
    risk = _RiskRepository()
    with pytest.raises(V1469PaidClaimConflictError):
        await V1469PaidCloseRuntime(claims, risk).record_close(
            claim_id="claim-1",
            fee_net_pnl_usdc=-0.05,
            terminal_reason="SL",
            occurred_at_ms=200,
            source_run_id="run-1",
        )
    assert claims.claim.status == "SUBMITTED"
    assert len(risk.calls) == 1


@pytest.mark.asyncio
async def test_terminal_replay_repairs_missing_daily_risk_without_mutation() -> None:
    claims = _ClaimRepository(_claim("TERMINAL"))
    risk = _RiskRepository()
    runtime = V1469PaidCloseRuntime(claims, risk)
    first = await runtime.record_close(
        claim_id="claim-1",
        fee_net_pnl_usdc=0.02,
        terminal_reason="TP",
        occurred_at_ms=200,
        source_run_id="run-1",
    )
    second = await runtime.record_close(
        claim_id="claim-1",
        fee_net_pnl_usdc=0.02,
        terminal_reason="TP",
        occurred_at_ms=200,
        source_run_id="run-1",
    )
    assert first.replayed and first.risk_event_inserted
    assert second.replayed and not second.risk_event_inserted
    assert not claims.terminal_calls


@pytest.mark.asyncio
async def test_hard_loss_marker_is_durable_and_conflicting_replay_fails_closed() -> None:
    claims = _ClaimRepository(_claim())
    risk = _RiskRepository()
    runtime = V1469PaidCloseRuntime(claims, risk)
    await runtime.record_close(
        claim_id="claim-1",
        fee_net_pnl_usdc=-0.05,
        terminal_reason="SL",
        occurred_at_ms=200,
        source_run_id="run-1",
        hard_loss_marker=True,
    )
    assert claims.claim.result_payload["hard_loss_marker"] is True
    with pytest.raises(V1469PaidClaimConflictError, match="facts differ"):
        await runtime.record_close(
            claim_id="claim-1",
            fee_net_pnl_usdc=-0.04,
            terminal_reason="SL",
            occurred_at_ms=200,
            source_run_id="run-1",
            hard_loss_marker=True,
        )


@pytest.mark.asyncio
async def test_close_rejects_unsubmitted_or_missing_claim() -> None:
    risk = _RiskRepository()
    with pytest.raises(V1469PaidClaimConflictError, match="SUBMITTED"):
        await V1469PaidCloseRuntime(
            _ClaimRepository(_claim("UNKNOWN")),
            risk,
        ).record_close(
            claim_id="claim-1",
            fee_net_pnl_usdc=0.0,
            terminal_reason="FLAT",
            occurred_at_ms=200,
            source_run_id="run-1",
        )


@pytest.mark.asyncio
async def test_proven_no_fill_terminalizes_without_paid_risk_event() -> None:
    claims = _ClaimRepository(_claim())
    risk = _RiskRepository()
    runtime = V1469PaidCloseRuntime(claims, risk)

    result = await runtime.record_no_fill(
        claim_id="claim-1",
        terminal_reason="ENTRY_EXPIRED",
        occurred_at_ms=200,
        source_run_id="run-1",
    )

    assert result.claim.status == "TERMINAL"
    assert risk.calls == []
    assert claims.claim.result_payload == {
        "schema": "v1469.paid-no-fill.1",
        "outcome": "NO_FILL",
        "source_run_id": "run-1",
    }


@pytest.mark.asyncio
async def test_no_fill_replay_is_exact_and_ambiguous_claim_stays_reserved() -> None:
    claims = _ClaimRepository(_claim())
    runtime = V1469PaidCloseRuntime(claims, _RiskRepository())
    await runtime.record_no_fill(
        claim_id="claim-1",
        terminal_reason="ENTRY_EXPIRED",
        occurred_at_ms=200,
        source_run_id="run-1",
    )
    replay = await runtime.record_no_fill(
        claim_id="claim-1",
        terminal_reason="ENTRY_EXPIRED",
        occurred_at_ms=999,
        source_run_id="run-1",
    )
    assert replay.replayed

    unknown = _ClaimRepository(_claim("UNKNOWN"))
    with pytest.raises(V1469PaidClaimConflictError, match="SUBMITTED"):
        await V1469PaidCloseRuntime(
            unknown, _RiskRepository()
        ).record_no_fill(
            claim_id="claim-1",
            terminal_reason="ENTRY_EXPIRED",
            occurred_at_ms=200,
            source_run_id="run-1",
        )
    assert unknown.claim.status == "UNKNOWN"