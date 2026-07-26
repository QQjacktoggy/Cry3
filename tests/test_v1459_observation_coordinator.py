from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json

import pytest

from src.gridbot.mainnet.runtime_identity import RuntimeIdentity
from src.gridbot.mainnet.shadow_simulator_v3_engine import (
    CoverageIntervalV3,
    FeeScheduleV3,
    ShadowTickV3,
    ShadowTradeSpecV3,
    TargetLevelV3,
    VerifiedCoverageV3,
    simulate_shadow_v3,
)
from src.gridbot.mainnet.v1459_observation_contract import (
    ObservationContractError,
    V1459ObservationFlags,
    V1459SessionCheckpoint,
)
from src.gridbot.mainnet.v1459_observation_coordinator import (
    V1459ObservationCoordinator,
)


def _identity(**updates) -> RuntimeIdentity:
    values = {
        "environment": "mainnet",
        "exchange_endpoint": "https://fapi.binance.com",
        "exchange_testnet": False,
        "account_fingerprint": "account-sha",
        "db_namespace": "gridbot_mainnet.db",
        "symbol": "ETHUSDC",
        "account_mode": "ONE_WAY",
        "deployment_commit": "commit-a",
        "config_hash": "config-a",
    }
    values.update(updates)
    return RuntimeIdentity(**values)


class _EvidenceRepo:
    def __init__(self) -> None:
        self.sessions: list[dict] = []
        self.session_rows: dict[str, dict] = {}
        self.opportunities: dict[tuple[str, str], dict] = {}

    async def upsert_session(self, payload: dict) -> bool:
        existing = self.session_rows.get(payload["session_id"])
        if existing is not None and payload["revision"] <= existing["revision"]:
            return False
        stored = dict(payload)
        for target, source in (
            ("counters_json", "counters"),
            ("disabled_states_json", "disabled_states"),
            ("route_stats_json", "route_stats"),
        ):
            stored[target] = json.dumps(
                payload.get(source),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        self.sessions.append(stored)
        self.session_rows[payload["session_id"]] = stored
        return True

    async def get_session(self, session_id: str):
        return self.session_rows.get(session_id)

    async def record_opportunity(self, payload: dict) -> bool:
        key = (payload["session_id"], payload["opportunity_id"])
        if key in self.opportunities:
            return False
        stored = dict(payload)
        for target, source in (
            ("feature_snapshot_json", "feature_snapshot"),
            ("feature_timestamps_json", "feature_timestamps"),
            ("action_schema_json", "action_schema"),
            ("raw_decision_json", "raw_decision"),
            ("effective_decision_json", "effective_decision"),
        ):
            stored[target] = json.dumps(
                payload.get(source, {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        self.opportunities[key] = stored
        return True

    async def get_opportunity(self, session_id: str, opportunity_id: str):
        return self.opportunities.get((session_id, opportunity_id))


class _ResultRepo:
    def __init__(self) -> None:
        self.shadows: list[dict] = []
        self.reconciliations: list[tuple[dict, list, list]] = []

    async def record_shadow_evaluation(self, payload: dict) -> bool:
        self.shadows.append(dict(payload))
        return True

    async def record_reconciliation(self, payload: dict, *, trades=(), incomes=()) -> bool:
        self.reconciliations.append((dict(payload), list(trades), list(incomes)))
        return True


def _coordinator(flags: V1459ObservationFlags):
    evidence, results = _EvidenceRepo(), _ResultRepo()
    return V1459ObservationCoordinator(
        flags=flags, evidence_repo=evidence, result_repo=results
    ), evidence, results


@pytest.mark.asyncio
async def test_flags_disable_every_write_and_never_permit_orders() -> None:
    with pytest.raises(ObservationContractError, match="parent"):
        V1459ObservationFlags(record_shadow=True)
    coordinator, evidence, results = _coordinator(V1459ObservationFlags())
    checkpoint = V1459SessionCheckpoint(
        "session-a", _identity(), _identity(), "v1.4.59", 0, 1_000, 1_000
    )
    write = await coordinator.persist_checkpoint(checkpoint)
    assert not write.attempted and evidence.sessions == [] and results.shadows == []
    assert coordinator.permits_order_mutation is False


@pytest.mark.asyncio
async def test_identity_mismatch_is_durably_paused_and_clears_rearm() -> None:
    flags = V1459ObservationFlags(enabled=True, persist_session=True)
    coordinator, evidence, _ = _coordinator(flags)
    checkpoint = V1459SessionCheckpoint(
        session_id="session-a",
        expected_identity=_identity(),
        observed_identity=_identity(config_hash="wrong"),
        code_version="v1.4.59",
        revision=3,
        started_at_ms=1_000,
        checkpoint_at_ms=2_000,
        rearm_pending=True,
        counters={"accepted": 2, "blocked": 5},
    )
    result = await coordinator.persist_checkpoint(checkpoint)
    payload = evidence.sessions[-1]
    assert result.status == "PAUSED_REQUIRES_ACK"
    assert result.reason == "config_hash_mismatch"
    assert payload["pause_reason"] == "config_hash_mismatch"
    assert payload["rearm_pending"] is False and payload["revision"] == 3


@pytest.mark.asyncio
async def test_stopped_checkpoint_releases_the_open_session_scope() -> None:
    flags = V1459ObservationFlags(enabled=True, persist_session=True)
    coordinator, evidence, _ = _coordinator(flags)
    checkpoint = V1459SessionCheckpoint(
        session_id="session-a",
        expected_identity=_identity(),
        observed_identity=_identity(),
        code_version="v1.4.59",
        revision=4,
        started_at_ms=1_000,
        checkpoint_at_ms=2_000,
        rearm_pending=True,
        stopped_at_ms=2_000,
        stop_reason="manual_stop",
    )

    result = await coordinator.persist_checkpoint(checkpoint)

    payload = evidence.sessions[-1]
    assert result.status == "STOPPED"
    assert result.reason is None
    assert payload["status"] == "STOPPED"
    assert payload["rearm_pending"] is False
    assert payload["pause_reason"] is None


@pytest.mark.asyncio
async def test_identical_checkpoint_retry_is_safe_but_same_revision_conflict_fails() -> None:
    flags = V1459ObservationFlags(enabled=True, persist_session=True)
    coordinator, _, _ = _coordinator(flags)
    checkpoint = V1459SessionCheckpoint(
        "session-a",
        _identity(),
        _identity(),
        "v1.4.59",
        0,
        1_000,
        2_000,
        counters={"accepted": 1},
    )
    first = await coordinator.persist_checkpoint(checkpoint)
    retry = await coordinator.persist_checkpoint(checkpoint)
    assert first.inserted and not retry.inserted
    assert retry.status == "ACTIVE" and retry.reason == "IDEMPOTENT_RETRY"
    with pytest.raises(ObservationContractError, match="conflicting session"):
        await coordinator.persist_checkpoint(replace(checkpoint, counters={"accepted": 2}))


@pytest.mark.asyncio
async def test_accepted_and_blocked_opportunities_are_immutable() -> None:
    flags = V1459ObservationFlags(enabled=True, record_opportunities=True)
    coordinator, _, _ = _coordinator(flags)
    base = {
        "session_id": "session-a",
        "opportunity_id": "opp-a",
        "observed_at_ms": 1_000,
        "decision_at_ms": 999,
        "source_run_id": "run-a",
        "opportunity_bucket": 16,
        "feature_hash": "feature-a",
        "feature_snapshot": {"rng15_bp": 20.0},
        "feature_timestamps": {"rng15_bp": 998},
        "evidence_contract_version": "v1459-opportunity-evidence-v2",
        "outcome_blind": True,
        "symbol": "ETHUSDC",
        "side": "SHORT",
        "lane_code": "STUP-S",
        "market_state": "clean_extension",
        "reject_reason": None,
        "promotion_source": None,
        "decision_schema_version": "v1.4.59",
        "action_schema": {"entry": "E2"},
        "raw_decision": {"accepted": False},
        "effective_decision": {"accepted": False},
        "quality_status": "OBSERVED",
    }
    first = await coordinator.persist_opportunity(base)
    retry = await coordinator.persist_opportunity(base)
    assert first.inserted and not retry.inserted
    assert first.status == "BLOCKED_OBSERVED"
    accepted = dict(base, opportunity_id="opp-b")
    accepted["effective_decision"] = {"accepted": True}
    assert (await coordinator.persist_opportunity(accepted)).status == "ACCEPTED_OBSERVED"
    with pytest.raises(ObservationContractError, match="conflicting immutable"):
        await coordinator.persist_opportunity(dict(base, feature_hash="changed"))


def _no_fill_outcome():
    fees = FeeScheduleV3(
        Decimal("0.0002"), Decimal("0.0002"), Decimal("0.0005"),
        Decimal("0.0005"), Decimal("1"), Decimal("1"), Decimal("0"),
        "explicit-test-fees", "explicit-test-funding",
    )
    spec = ShadowTradeSpecV3(
        opportunity_id="opp-a", variant="E2", fill_model="TRADE_THROUGH",
        simulation_version="v1.4.59-fixed-v3", side="BUY", start_ms=1_000,
        decision_latency_ms=250, entry_ttl_ms=500, outcome_deadline_ms=2_000,
        signal_price=Decimal("100"), tick_size=Decimal("1"), quantity=Decimal("0.5"),
        tp=TargetLevelV3("ABSOLUTE", absolute_price=Decimal("110")),
        sl=TargetLevelV3("ABSOLUTE", absolute_price=Decimal("90")), fees=fees,
    )
    coverage = VerifiedCoverageV3(
        (CoverageIntervalV3(1_000, 1_500, 1_600, "sentinel-proof"),), "cache-sha"
    )
    return simulate_shadow_v3(
        spec, (ShadowTickV3(1_600, 10, Decimal("105")),), coverage
    )


@pytest.mark.asyncio
async def test_complete_no_fill_persists_zero_ev_contract_without_fake_pnl() -> None:
    flags = V1459ObservationFlags(enabled=True, record_shadow=True)
    coordinator, _, results = _coordinator(flags)
    outcome = _no_fill_outcome()
    write = await coordinator.persist_shadow(
        "session-a", outcome, recorded_at_ms=3_000, extra_input={"source": "test"}
    )
    row = results.shadows[-1]
    assert write.status == "COMPLETE"
    assert row["fill_status"] == "UNFILLED_EXPIRED"
    assert row["net_pnl_usdc"] is None and row["partial_fill_ratio"] == 0
    assert row["input"]["ev_opportunity_contribution_usdc"] == "0"
    assert row["input"]["metric_contract"] == outcome.metric_contract


@pytest.mark.asyncio
async def test_reconciliation_result_and_exchange_ids_are_atomic_inputs() -> None:
    flags = V1459ObservationFlags(enabled=True, record_reconciliation=True)
    coordinator, _, results = _coordinator(flags)
    raw_trades = [
        {"exchange_trade_id": "entry", "owned": True, "role": "ENTRY", "is_maker": True,
         "realized_pnl_usdc": 0.0, "commission_amount": 0.01, "commission_asset": "USDC"},
        {"exchange_trade_id": "exit", "owned": True, "role": "EXIT", "is_maker": False,
         "realized_pnl_usdc": 0.20, "commission_amount": 0.01, "commission_asset": "USDC"},
    ]
    persisted = [
        {"exchange_trade_id": row["exchange_trade_id"], "order_id": row["exchange_trade_id"],
         "role": row["role"], "is_maker": row["is_maker"],
         "realized_pnl_usdc": row["realized_pnl_usdc"],
         "commission_amount": row["commission_amount"], "commission_asset": "USDC",
         "commission_usdc": row["commission_amount"], "source": {"owned": True}}
        for row in raw_trades
    ]
    reconciliation, write = await coordinator.reconcile_and_persist(
        trades=raw_trades, incomes=(), persistence_trades=persisted,
        persistence_incomes=(), run_id="run-a", reconciliation_revision=0,
        environment="mainnet", account_fingerprint="account-sha", symbol="ETHUSDC",
        reconciled_at_ms=5_000, source={"collector": "test"},
    )
    assert reconciliation.reconciliation_status == "COMPLETE"
    assert reconciliation.net_pnl_usdc == pytest.approx(0.18)
    assert write.inserted and results.reconciliations[-1][0]["net_pnl_usdc"] == pytest.approx(0.18)
    with pytest.raises(ObservationContractError, match="trade IDs"):
        await coordinator.reconcile_and_persist(
            trades=raw_trades, incomes=(), persistence_trades=persisted[:1],
            persistence_incomes=(), run_id="run-b", reconciliation_revision=0,
            environment="mainnet", account_fingerprint="account-sha", symbol="ETHUSDC",
            reconciled_at_ms=5_001,
        )
