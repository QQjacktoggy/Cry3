from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

import src.gridbot.mainnet.v1469_authority_runtime as runtime_module
from src.gridbot.mainnet.v1469_adaptive_identity import (
    EXECUTION_PROFILE_SCHEMA,
)
from src.gridbot.mainnet.v1469_arbiter_evidence_mapper import (
    DurableEvidenceMapping,
)
from src.gridbot.mainnet.v1469_arm_arbiter import (
    ArmCandidate,
    ArmEvidence,
    ArmIdentity,
    EvidenceOutcome,
    LeaseAction,
    LeasePhase,
    LeaseProposal,
    RegimeSnapshot,
)
from src.gridbot.mainnet.v1469_arm_profiles import (
    LEGACY_CONTROL,
    PASSIVE_BALANCED,
    RANGE_SCALP,
    get_arm_profile,
)
from src.gridbot.mainnet.v1469_authority_runtime import (
    AuthorityRuntimeInput,
    LeaseApplyRequest,
    V1469AuthorityRuntime,
)
from src.gridbot.storage.database import Database
from src.gridbot.storage.v1469_arm_observation_repository import arm_identity
from src.gridbot.storage.v1469_lease_repository import (
    LeaseContext,
    V1469LeaseRepository,
)


NOW = 2_000_000_000
MINUTE = 60_000
ENVIRONMENT = "MAINNET"
SYMBOL = "ETHUSDC"
OPPORTUNITY_ID = "current-opportunity"


class _ObservationRepository:
    def __init__(
        self,
        bundle: dict[str, Any],
        *,
        scope_complete: bool = True,
        load_error: Exception | None = None,
    ) -> None:
        self.bundle = bundle
        self.scope_complete = scope_complete
        self.load_error = load_error
        self.bundle_calls: list[str] = []
        self.ledger_calls: list[dict[str, Any]] = []

    async def load_observation_bundle(
        self, opportunity_id: str
    ) -> dict[str, Any] | None:
        self.bundle_calls.append(opportunity_id)
        return self.bundle

    async def durable_terminal_evidence_ledger(
        self,
        *,
        environment: str,
        symbol: str,
        as_of_ms: int,
        limit: int,
    ) -> dict[str, Any]:
        if self.load_error is not None:
            raise self.load_error
        self.ledger_calls.append(
            {
                "environment": environment,
                "symbol": symbol,
                "as_of_ms": as_of_ms,
                "limit": limit,
            }
        )
        rows = [{"durable": "sentinel"}] if self.scope_complete else []
        return {
            "rows": rows,
            "scope_complete": self.scope_complete,
            "row_count": len(rows),
            "limit": limit,
            "truncated": not self.scope_complete,
            "as_of_ms": as_of_ms,
        }


class _UnexpectedLeaseRepository:
    async def get_active_lease(self, **kwargs):  # pragma: no cover - assertion
        raise AssertionError("lease repository must not be consulted")

    async def get_lease(self, arm_key):  # pragma: no cover - assertion
        raise AssertionError("lease repository must not be consulted")

    async def apply_proposal(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("lease repository must not be consulted")


def _regime(*, age_ms: int = 20_000) -> RegimeSnapshot:
    observed = NOW - age_ms
    return RegimeSnapshot(
        regime="RANGE",
        observed_at_ms=observed,
        confirmation_at_ms=(observed - 20_000, observed),
        direction_valid_sides=frozenset({"LONG"}),
    )


def _submit() -> RegimeSnapshot:
    return RegimeSnapshot(
        regime="RANGE",
        observed_at_ms=NOW - 5_000,
        direction_valid_sides=frozenset({"LONG"}),
    )


def _identity(
    profile_id: str = PASSIVE_BALANCED,
    *,
    lane_code: str = "W6A",
    strategy: str = "S1_BB_RSI",
    profile_hash: str | None = None,
) -> ArmIdentity:
    if profile_id == LEGACY_CONTROL:
        resolved_hash = profile_hash or "legacy-profile-hash"
    else:
        resolved_hash = get_arm_profile(profile_id).execution_profile_hash
    arm_key = arm_identity(
        {
            "lane_code": lane_code,
            "effective_side": "LONG",
            "strategy": strategy,
            "coarse_regime": "RANGE",
            "execution_profile_id": profile_id,
            "execution_profile_schema": EXECUTION_PROFILE_SCHEMA,
            "execution_profile_hash": resolved_hash,
        }
    )
    return ArmIdentity(
        arm_key=arm_key,
        lane_code=lane_code,
        side="LONG",
        strategy=strategy,
        regime="RANGE",
        execution_profile_id=profile_id,
        execution_profile_hash=resolved_hash,
    )


def _candidate(
    profile_id: str = PASSIVE_BALANCED,
    *,
    reward_bp: float = 4.0,
    lane_code: str = "W6A",
) -> ArmCandidate:
    identity = _identity(profile_id, lane_code=lane_code)
    specifications = (
        ("history-1", 1 * MINUTE, EvidenceOutcome.TP_FIRST, reward_bp),
        ("history-2", 2 * MINUTE, EvidenceOutcome.TP_FIRST, reward_bp),
        ("history-3", 3 * MINUTE, EvidenceOutcome.TP_FIRST, reward_bp),
        ("history-4", 4 * MINUTE, EvidenceOutcome.NO_FILL, 0.0),
        ("history-5", 60 * MINUTE, EvidenceOutcome.MAX_HOLD, 0.0),
        ("history-6", 180 * MINUTE, EvidenceOutcome.MAX_HOLD, 0.0),
    )
    evidence = tuple(
        ArmEvidence(
            arm_key=identity.arm_key,
            opportunity_id=opportunity_id,
            observed_at_ms=NOW - age_ms,
            terminal_at_ms=NOW - age_ms + 500,
            deadline_at_ms=NOW - age_ms + 1_000,
            outcome=outcome,
            reward_net_bp=reward,
            regime="RANGE",
            paired=True,
            evaluable=True,
            data_complete=True,
            identity_valid=True,
        )
        for opportunity_id, age_ms, outcome, reward in specifications
    )
    return ArmCandidate(
        identity=identity,
        evidence=evidence,
        source_evidence_revision=f"revision-{profile_id.lower()}",
    )


def _bundle(
    *,
    lane_code: str = "W6A",
    strategy: str = "S1_BB_RSI",
    safety_status: str = "SAFE",
    data_complete: bool = True,
) -> dict[str, Any]:
    return {
        "opportunity": {
            "opportunity_id": OPPORTUNITY_ID,
            "environment": ENVIRONMENT,
            "symbol": SYMBOL,
            "observed_at_ms": NOW - 6_000,
            "feature_at_ms": NOW - 6_500,
            "coarse_regime": "RANGE",
            "data_quality": "COMPLETE",
            "feature_snapshot": {"market_state": "range"},
        },
        "candidates": (
            {
                "candidate_id": "current-candidate",
                "opportunity_id": OPPORTUNITY_ID,
                "lane_code": lane_code,
                "effective_side": "LONG",
                "strategy": strategy,
                "match_status": "MATCH",
                "safety_status": safety_status,
                "data_complete": data_complete,
                "is_selected": False,
            },
        ),
    }


def _runtime_input(
    *,
    current_lease=None,
    incumbent_arm_key: str | None = None,
    regime_snapshot: RegimeSnapshot | None = None,
) -> AuthorityRuntimeInput:
    return AuthorityRuntimeInput(
        environment="mainnet",
        symbol="ethusdc",
        opportunity_id=OPPORTUNITY_ID,
        as_of_ms=NOW,
        regime_snapshot=regime_snapshot or _regime(),
        submit_snapshot=_submit(),
        current_lease=current_lease,
        incumbent_arm_key=incumbent_arm_key,
        ledger_limit=37,
    )


def _mapping(
    monkeypatch: pytest.MonkeyPatch,
    *candidates: ArmCandidate,
) -> None:
    result = DurableEvidenceMapping(
        candidates=tuple(candidates),
        issues=(),
        ledger_revision="ledger-revision",
        durable_rows=1,
        trusted_paired_rows=sum(len(item.evidence) for item in candidates),
    )

    def exact_mapper(rows, *, ledger_scope_complete):
        assert rows == [{"durable": "sentinel"}]
        assert ledger_scope_complete is True
        return result

    monkeypatch.setattr(
        runtime_module, "map_durable_paired_evidence", exact_mapper
    )


def _lease_context(*, evidence_as_of_ms: int = NOW - 1_000) -> LeaseContext:
    return LeaseContext(
        environment=ENVIRONMENT,
        symbol=SYMBOL,
        execution_profile_schema=EXECUTION_PROFILE_SCHEMA,
        notional_cap_usdc=20.0,
        risk_policy_hash="risk-policy-hash",
        evidence_as_of_ms=evidence_as_of_ms,
        owner_id="owner-a",
        boot_id="boot-a",
    )


async def _lease_repository(
    tmp_path: Path,
) -> tuple[Database, V1469LeaseRepository]:
    database = Database(str(tmp_path / "authority-runtime.db"))
    await database.initialize()
    return database, V1469LeaseRepository(database)


async def _seed_active_lease(
    repository: V1469LeaseRepository,
    candidate: ArmCandidate,
):
    result = await repository.apply_proposal(
        candidate.identity,
        LeaseProposal(
            action=LeaseAction.GRANT,
            arm_key=candidate.identity.arm_key,
            phase=LeasePhase.PROBATION,
            evidence_revision=candidate.source_evidence_revision,
            expires_at_ms=NOW + 60_000,
        ),
        _lease_context(evidence_as_of_ms=NOW - 2_000),
        expected_generation=0,
        expected_evidence_revision=None,
        event_time_ms=NOW - 1_000,
        idempotency_key="seed-active-lease",
        actor="test",
    )
    return result.lease


@pytest.mark.asyncio
async def test_bounded_runtime_preserves_explicit_incumbent_and_uses_repo_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incumbent = _candidate(PASSIVE_BALANCED, reward_bp=4.0)
    challenger = _candidate(RANGE_SCALP, reward_bp=5.0)
    _mapping(monkeypatch, challenger, incumbent)
    observation = _ObservationRepository(_bundle())
    database, leases = await _lease_repository(tmp_path)
    try:
        durable = await _seed_active_lease(leases, incumbent)
        result = await V1469AuthorityRuntime(
            observation, leases
        ).evaluate(
            _runtime_input(
                current_lease=durable,
                incumbent_arm_key=incumbent.identity.arm_key,
            )
        )

        assert result.submit_admissible is True
        assert result.winner == incumbent.identity
        assert result.arbiter_request is not None
        assert (
            result.arbiter_request.incumbent_arm_key
            == incumbent.identity.arm_key
        )
        assert result.current_opportunity is not None
        assert result.current_opportunity.candidate_id == "current-candidate"
        assert result.durable_lease == await leases.get_active_lease(
            environment=ENVIRONMENT,
            symbol=SYMBOL,
            now_ms=NOW,
        )
        assert observation.bundle_calls == [OPPORTUNITY_ID]
        assert observation.ledger_calls == [
            {
                "environment": ENVIRONMENT,
                "symbol": SYMBOL,
                "as_of_ms": NOW,
                "limit": 37,
            }
        ]
        with pytest.raises(FrozenInstanceError):
            result.submit_admissible = False  # type: ignore[misc]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_spoofed_caller_durable_lease_never_becomes_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate()
    _mapping(monkeypatch, candidate)
    database, leases = await _lease_repository(tmp_path)
    try:
        durable = await _seed_active_lease(leases, candidate)
        spoofed = replace(durable, owner_id="spoofed-owner")
        result = await V1469AuthorityRuntime(
            _ObservationRepository(_bundle()), leases
        ).evaluate(
            _runtime_input(
                current_lease=spoofed,
                incumbent_arm_key=candidate.identity.arm_key,
            )
        )

        assert result.submit_admissible is False
        assert result.blockers == ("caller_current_lease_mismatch",)
        assert result.durable_lease == durable
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_initial_grant_uses_explicit_context_and_reloads_durable_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate()
    _mapping(monkeypatch, candidate)
    database, leases = await _lease_repository(tmp_path)
    context = _lease_context()
    try:
        result = await V1469AuthorityRuntime(
            _ObservationRepository(_bundle()), leases
        ).evaluate(
            _runtime_input(),
            lease_apply=LeaseApplyRequest(
                context=context,
                idempotency_key="runtime-grant",
                actor="authority-runtime-test",
            ),
        )

        assert result.submit_admissible is True
        assert result.decision.lease_proposal.action is LeaseAction.GRANT
        assert result.lease_mutation is not None
        assert result.lease_mutation.applied is True
        assert result.durable_lease is not None
        assert result.durable_lease.notional_cap_usdc == pytest.approx(20.0)
        assert result.durable_lease.risk_policy_hash == "risk-policy-hash"
        assert result.durable_lease == await leases.get_active_lease(
            environment=ENVIRONMENT,
            symbol=SYMBOL,
            now_ms=NOW,
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_lease_apply_rejects_changed_preview_identity_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate()
    _mapping(monkeypatch, candidate)
    database, leases = await _lease_repository(tmp_path)
    try:
        result = await V1469AuthorityRuntime(
            _ObservationRepository(_bundle()), leases
        ).evaluate(
            _runtime_input(),
            lease_apply=LeaseApplyRequest(
                context=_lease_context(),
                idempotency_key="runtime-stale-preview",
                actor="authority-runtime-test",
                expected_arm_key="v1469a_" + "0" * 64,
            ),
        )

        assert result.submit_admissible is False
        assert result.blockers == ("lease_apply_decision_changed",)
        assert await leases.get_active_lease(
            environment=ENVIRONMENT,
            symbol=SYMBOL,
            now_ms=NOW,
        ) is None
    finally:
        await database.close()
@pytest.mark.asyncio
async def test_current_opportunity_mismatch_blocks_before_any_lease_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate(lane_code="W6A")
    _mapping(monkeypatch, candidate)
    result = await V1469AuthorityRuntime(
        _ObservationRepository(_bundle(lane_code="W1A")),
        _UnexpectedLeaseRepository(),  # type: ignore[arg-type]
    ).evaluate(_runtime_input())

    assert result.submit_admissible is False
    assert result.blockers == ("winner_not_in_current_candidates",)
    assert result.winner == candidate.identity
    assert result.current_opportunity is None


@pytest.mark.asyncio
async def test_incomplete_durable_ledger_fails_closed_before_arbiter_or_lease(
) -> None:
    observation = _ObservationRepository(
        _bundle(), scope_complete=False
    )
    result = await V1469AuthorityRuntime(
        observation,
        _UnexpectedLeaseRepository(),  # type: ignore[arg-type]
    ).evaluate(_runtime_input())

    assert result.submit_admissible is False
    assert result.blockers == ("durable_ledger_scope_incomplete",)
    assert result.winner is None
    assert result.evidence_mapping is not None
    assert result.evidence_mapping.candidates == ()
    assert result.ledger_scope_complete is False


@pytest.mark.asyncio
async def test_stale_regime_snapshot_is_rejected_by_pure_arbiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _candidate()
    _mapping(monkeypatch, candidate)
    result = await V1469AuthorityRuntime(
        _ObservationRepository(_bundle()),
        _UnexpectedLeaseRepository(),  # type: ignore[arg-type]
    ).evaluate(
        _runtime_input(regime_snapshot=_regime(age_ms=60_001))
    )

    assert result.submit_admissible is False
    assert result.blockers[0] == "arbiter_no_winner"
    assert "regime_snapshot_stale" in result.blockers
    assert result.arbiter_request is not None
    assert result.arbiter_request.regime_snapshot.observed_at_ms == (
        NOW - 60_001
    )


@pytest.mark.asyncio
async def test_legacy_arm_remains_incumbent_but_not_adaptive_submit_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _candidate(LEGACY_CONTROL)
    _mapping(monkeypatch, legacy)
    result = await V1469AuthorityRuntime(
        _ObservationRepository(_bundle()),
        _UnexpectedLeaseRepository(),  # type: ignore[arg-type]
    ).evaluate(
        _runtime_input(incumbent_arm_key=legacy.identity.arm_key)
    )

    assert result.winner == legacy.identity
    assert result.submit_admissible is False
    assert result.blockers == (
        "legacy_control_requires_legacy_paid_path",
    )

class _PaidEvidenceSource:
    def __init__(
        self,
        database: Database,
        *,
        hard_loss_marker: bool = False,
    ) -> None:
        self.database = database
        self.hard_loss_marker = hard_loss_marker

    async def load_paid_probation_evidence(self, **kwargs):
        await self.database.conn.execute(
            """INSERT INTO v1469_paid_promotion_evidence_snapshots (
                environment, symbol, arm_key, execution_profile_hash,
                regime, evidence_revision, window_start_ms, as_of_ms,
                evidence_limit, clock_revision, evidence_watermark,
                terminal_fills, wins, fee_net_paid_pnl, hard_loss_marker,
                latest_terminal_at_ms, truncated, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 3, 2, 0.03, ?,
                      ?, 0, ?)""",
            (
                kwargs["environment"],
                kwargs["symbol"],
                kwargs["arm_key"],
                kwargs["execution_profile_hash"],
                kwargs["regime"],
                kwargs["evidence_revision"],
                kwargs["window_start_ms"],
                kwargs["as_of_ms"],
                kwargs["limit"],
                "e" * 64,
                int(self.hard_loss_marker),
                kwargs["as_of_ms"] - 1,
                kwargs["as_of_ms"],
            ),
        )
        await self.database.conn.commit()
        return {
            "terminal_fills": 3,
            "wins": 2,
            "fee_net_paid_pnl": 0.03,
            "hard_loss_marker": self.hard_loss_marker,
            "evidence_watermark": "e" * 64,
            "evidence_snapshot_durable": True,
        }


@pytest.mark.asyncio
async def test_ready_paid_evidence_uses_durable_live_promotion_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.gridbot.mainnet.v1469_paid_promotion_runtime import (
        V1469PaidPromotionRuntime,
    )

    candidate = _candidate(PASSIVE_BALANCED)
    _mapping(monkeypatch, candidate)
    database, leases = await _lease_repository(tmp_path)
    try:
        durable = await _seed_active_lease(leases, candidate)
        runtime = V1469AuthorityRuntime(
            _ObservationRepository(_bundle()),
            leases,
            promotion_runtime=V1469PaidPromotionRuntime(
                _PaidEvidenceSource(database), evidence_window_ms=45 * MINUTE
            ),
        )
        result = await runtime.evaluate(
            _runtime_input(
                current_lease=durable,
                incumbent_arm_key=candidate.identity.arm_key,
            ),
            lease_apply=LeaseApplyRequest(
                context=replace(
                    _lease_context(evidence_as_of_ms=NOW),
                    notional_cap_usdc=40.0,
                ),
                idempotency_key="durable-live-promotion",
                actor="test",
            ),
        )
        assert result.submit_admissible is True
        assert result.lease_mutation is not None
        assert result.lease_mutation.applied is True
        assert result.durable_lease is not None
        assert result.durable_lease.phase is LeasePhase.LIVE
        assert result.durable_lease.generation == durable.generation + 1
        assert await database.fetchone(
            """SELECT COUNT(*) AS n FROM v1469_arm_events
            WHERE event_type = 'LIVE_PROMOTED'"""
        ) == {"n": 1}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_hard_loss_paid_evidence_never_upgrades_probation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.gridbot.mainnet.v1469_paid_promotion_runtime import (
        V1469PaidPromotionRuntime,
    )

    candidate = _candidate(PASSIVE_BALANCED)
    _mapping(monkeypatch, candidate)
    database, leases = await _lease_repository(tmp_path)
    try:
        durable = await _seed_active_lease(leases, candidate)
        runtime = V1469AuthorityRuntime(
            _ObservationRepository(_bundle()),
            leases,
            promotion_runtime=V1469PaidPromotionRuntime(
                _PaidEvidenceSource(database, hard_loss_marker=True),
                evidence_window_ms=45 * MINUTE,
            ),
        )
        result = await runtime.evaluate(
            _runtime_input(
                current_lease=durable,
                incumbent_arm_key=candidate.identity.arm_key,
            ),
            lease_apply=LeaseApplyRequest(
                context=_lease_context(evidence_as_of_ms=NOW),
                idempotency_key="hard-loss-cannot-promote",
                actor="test",
            ),
        )
        assert result.submit_admissible is True
        assert result.durable_lease is not None
        assert result.durable_lease.phase is LeasePhase.PROBATION
        assert result.durable_lease.generation == durable.generation
        assert await database.fetchone(
            """SELECT COUNT(*) AS n FROM v1469_arm_events
            WHERE event_type = 'LIVE_PROMOTED'"""
        ) == {"n": 0}
    finally:
        await database.close()