from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.gridbot.mainnet.v1469_adaptive_identity import (
    BreakevenPolicy, DcaPolicy, EarlyFailPolicy, MarketStateIdentity,
    RepricePolicy, RunnerPolicy, TakeProfitLevel, TrailPolicy,
)
from src.gridbot.mainnet.v1469_legacy_control import LegacyExecutionSnapshot
from src.gridbot.mainnet.v1469_arbiter_evidence_mapper import (
    map_durable_paired_evidence,
)
from src.gridbot.mainnet.v1469_paired_evaluator import ShadowCostModel
from src.gridbot.mainnet.v1469_paired_shadow_runtime import (
    V1469PairedShadowRuntime,
)
from src.gridbot.storage.database import Database
from src.gridbot.storage.v1469_arm_observation_repository import (
    ArmEvidenceConflictError,
    V1469ArmObservationRepository,
)


OBSERVED_AT_MS = 1_000_000


def _opportunity() -> dict:
    return {
        "opportunity_id": "opp-paired-1",
        "environment": "MAINNET",
        "symbol": "BTCUSDC",
        "observed_at_ms": OBSERVED_AT_MS,
        "feature_at_ms": OBSERVED_AT_MS - 100,
        "coarse_regime": "RANGE",
        "regime_confidence": 0.9,
        "feature_schema": "v1469.feature.1",
        "feature_snapshot": {
            "market_state": "RANGE_STABLE",
            "signal_reference_price": 100.0,
        },
        "source_run_id": "run-paired",
        "source_event_id": "signal-paired-1",
        "data_quality": "COMPLETE",
        "created_at_ms": OBSERVED_AT_MS,
    }


def _candidate() -> dict:
    return {
        "opportunity_id": "opp-paired-1",
        "lane_code": "W6A",
        "effective_side": "LONG",
        "strategy": "S1_BB_RSI",
        "match_status": "MATCH",
        "safety_status": "NOT_EVALUATED",
        "is_selected": False,
        "selection_rank": 1,
        "suppression_reason": "LEGACY_FIRST_MATCH",
        "suppressed_by_lane_code": "W2A",
        "matcher_version": "v1469.match-all.2",
        "matcher_hash": "matcher-hash",
        "data_complete": True,
        "annotations": {"route_status": "SUPPRESSED"},
        "created_at_ms": OBSERVED_AT_MS,
    }


def _cost_model() -> ShadowCostModel:
    return ShadowCostModel(
        maker_fee_bp=0.0,
        taker_fee_bp=4.0,
        adverse_slippage_bp=1.0,
        provenance="test",
    )


def _legacy_snapshot() -> LegacyExecutionSnapshot:
    return LegacyExecutionSnapshot(
        market_identity=MarketStateIdentity(
            environment="MAINNET", symbol="BTCUSDC", lane_code="W6A",
            effective_side="LONG", strategy="S1_BB_RSI",
            coarse_regime="RANGE", market_state="RANGE_STABLE",
        ),
        entry_offset_bp=1.0, entry_type="LIMIT", entry_ttl_s=90,
        maker_mode="POST_ONLY",
        take_profits=(TakeProfitLevel(level_id="FULL", target_bp=8, fraction=1),),
        sl_bp=8, max_hold_s=360, reprice=RepricePolicy(),
        breakeven=BreakevenPolicy(), trail=TrailPolicy(),
        runner=RunnerPolicy(), early_fail=EarlyFailPolicy(), dca=DcaPolicy(),
        lane_notional_cap_usdc=25, global_notional_cap_usdc=50,
        risk_policy_hash="risk-a", reference_price=100,
    )


async def _repo(
    tmp_path: Path,
) -> tuple[Database, V1469ArmObservationRepository]:
    db = Database(str(tmp_path / "paired.db"))
    await db.initialize()
    return db, V1469ArmObservationRepository(db)


@pytest.mark.asyncio
async def test_legacy_control_does_not_skip_other_matched_lanes(
    tmp_path: Path,
) -> None:
    db, repo = await _repo(tmp_path)
    opportunity = _opportunity()
    w6a = _candidate()
    w2a = {
        **_candidate(),
        "lane_code": "W2A",
        "selection_rank": 2,
        "suppressed_by_lane_code": "W6A",
    }
    try:
        await repo.insert_observation(opportunity, [w6a, w2a])
        runtime = V1469PairedShadowRuntime(repo)
        started = await runtime.start_observation(
            {**opportunity, "legacy_execution_snapshot": _legacy_snapshot()},
            [w6a, w2a],
        )
        assert started == {
            "candidates_started": 2,
            "evidence_started": 5,
            "skipped": 0,
            "capacity_dropped": 0,
        }
        await runtime.advance(
            source_run_id="run-paired",
            agg_trade_rows=[
                {"a": 1, "T": OBSERVED_AT_MS + 100, "p": "99.90"},
                {"a": 2, "T": OBSERVED_AT_MS + 200, "p": "100.10"},
            ],
            coverage_start_ms=OBSERVED_AT_MS,
            coverage_end_ms=OBSERVED_AT_MS + 200,
            coverage_complete=True,
            force_data_failure=False,
            now_ms=OBSERVED_AT_MS + 500,
            cost_model=_cost_model(),
        )
        ledger = await repo.durable_terminal_evidence_ledger(
            environment="MAINNET",
            symbol="BTCUSDC",
            as_of_ms=OBSERVED_AT_MS + 1_000,
        )
        legacy_row = next(
            row
            for row in ledger["rows"]
            if row["execution_profile_id"] == "LEGACY_CONTROL"
        )
        assert json.loads(
            legacy_row["execution_profile_payload_json"]
        ) == _legacy_snapshot().to_payload()
        assert legacy_row["feature_snapshot"] == opportunity["feature_snapshot"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_suppressed_lane_gets_atomic_paired_evidence_and_shared_envelope(
    tmp_path: Path,
) -> None:
    db, repo = await _repo(tmp_path)
    opportunity, candidate = _opportunity(), _candidate()
    try:
        await repo.insert_observation(opportunity, [candidate])
        runtime = V1469PairedShadowRuntime(repo)
        started = await runtime.start_observation(
            opportunity, [candidate]
        )
        assert started == {
            "candidates_started": 1,
            "evidence_started": 2,
            "skipped": 0,
            "capacity_dropped": 0,
        }
        assert runtime.active_evidence_count == 2

        result = await runtime.advance(
            source_run_id="run-paired",
            agg_trade_rows=[
                {
                    "a": 1,
                    "T": OBSERVED_AT_MS + 100,
                    "p": "99.90",
                },
                {
                    "a": 2,
                    "T": OBSERVED_AT_MS + 200,
                    "p": "100.10",
                },
            ],
            coverage_start_ms=OBSERVED_AT_MS,
            coverage_end_ms=OBSERVED_AT_MS + 200,
            coverage_complete=True,
            force_data_failure=False,
            now_ms=OBSERVED_AT_MS + 500,
            cost_model=_cost_model(),
        )
        assert result == {
            "groups_terminal": 1,
            "evidence_terminal": 2,
            "groups_pending": 0,
            "errors": 0,
        }

        rows = await db.fetchall(
            """SELECT execution_profile_id, arm_key, status, outcome,
                      reward_net_bp, terminal_payload_json
            FROM v1469_arm_evidence
            ORDER BY execution_profile_id"""
        )
        assert len(rows) == 2
        assert {row["status"] for row in rows} == {"TERMINAL"}
        assert {row["outcome"] for row in rows} == {"tp_first"}
        payloads = [
            json.loads(row["terminal_payload_json"]) for row in rows
        ]
        assert len(
            {payload["envelope_hash"] for payload in payloads}
        ) == 1
        assert all(
            row["arm_key"] == payload["arm_hash"]
            for row, payload in zip(rows, payloads)
        )
        contracts = [
            payload["paired_contract"] for payload in payloads
        ]
        assert len(
            {contract["paired_group_id"] for contract in contracts}
        ) == 1
        assert all(
            contract["coverage_complete"] is True
            for contract in contracts
        )
        assert all(row["reward_net_bp"] > 0 for row in rows)

        ledger = await repo.durable_terminal_evidence_ledger(
            environment="MAINNET",
            symbol="BTCUSDC",
            as_of_ms=OBSERVED_AT_MS + 1_000,
        )
        mapped = map_durable_paired_evidence(
            ledger["rows"],
            ledger_scope_complete=bool(ledger["scope_complete"]),
        )
        assert len(mapped.candidates) == 2
        assert mapped.trusted_paired_rows == 2
        assert all(
            candidate.evidence[0].paired
            for candidate in mapped.candidates
        )

        truncated = await repo.durable_terminal_evidence_ledger(
            environment="MAINNET",
            symbol="BTCUSDC",
            as_of_ms=OBSERVED_AT_MS + 1_000,
            limit=1,
        )
        assert truncated["scope_complete"] is False
        blocked = map_durable_paired_evidence(
            truncated["rows"],
            ledger_scope_complete=False,
        )
        assert blocked.candidates == ()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_exact_legacy_requires_complete_durable_opportunity(
    tmp_path: Path,
) -> None:
    db, repo = await _repo(tmp_path)
    opportunity, candidate = _opportunity(), _candidate()
    try:
        await repo.insert_observation(opportunity, [candidate])
        runtime = V1469PairedShadowRuntime(repo)
        with pytest.raises(
            ValueError,
            match="legacy control requires COMPLETE durable opportunity",
        ):
            await runtime.start_observation(
                {
                    **opportunity,
                    "data_quality": "DATA_INCOMPLETE",
                    "legacy_execution_snapshot": _legacy_snapshot(),
                },
                [candidate],
            )
        assert await db.fetchone(
            "SELECT COUNT(*) AS n FROM v1469_arm_evidence"
        ) == {"n": 0}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_exact_legacy_reference_mismatch_fails_before_evidence(
    tmp_path: Path,
) -> None:
    db, repo = await _repo(tmp_path)
    opportunity, candidate = _opportunity(), _candidate()
    try:
        await repo.insert_observation(opportunity, [candidate])
        runtime = V1469PairedShadowRuntime(repo)
        with pytest.raises(
            ValueError,
            match="reference price does not match durable opportunity",
        ):
            await runtime.start_observation(
                {
                    **opportunity,
                    "legacy_execution_snapshot": replace(
                        _legacy_snapshot(),
                        reference_price=101.0,
                    ),
                },
                [candidate],
            )
        assert await db.fetchone(
            "SELECT COUNT(*) AS n FROM v1469_arm_evidence"
        ) == {"n": 0}
    finally:
        await db.close()

@pytest.mark.asyncio
async def test_legacy_control_identity_mismatch_fails_before_evidence(
    tmp_path: Path,
) -> None:
    db, repo = await _repo(tmp_path)
    opportunity = _opportunity()
    candidate = {**_candidate(), "lane_code": "W2A"}
    try:
        await repo.insert_observation(opportunity, [candidate])
        runtime = V1469PairedShadowRuntime(repo)
        with pytest.raises(
            ValueError,
            match="legacy control must match exactly one durable candidate",
        ):
            await runtime.start_observation(
                {
                    **opportunity,
                    "legacy_execution_snapshot": _legacy_snapshot(),
                },
                [candidate],
            )
        assert await db.fetchone(
            "SELECT COUNT(*) AS n FROM v1469_arm_evidence"
        ) == {"n": 0}
    finally:
        await db.close()

@pytest.mark.asyncio
async def test_capacity_drop_is_durable_and_fresh_runtime_rehydrates_exact_bundle(
    tmp_path: Path,
) -> None:
    db, repo = await _repo(tmp_path)
    opportunity, candidate = _opportunity(), _candidate()
    legacy = _legacy_snapshot()
    try:
        await repo.insert_observation(opportunity, [candidate])
        constrained = V1469PairedShadowRuntime(
            repo,
            max_active_evidence=2,
        )
        scope_marker = "scope:MAINNET:BTCUSDC"
        run_marker = "run:run-paired"
        constrained._rehydrated_runs.update({scope_marker, run_marker})
        dropped = await constrained.start_observation(
            {
                **opportunity,
                "legacy_execution_snapshot": legacy,
            },
            [candidate],
        )
        assert dropped == {
            "candidates_started": 0,
            "evidence_started": 3,
            "skipped": 0,
            "capacity_dropped": 3,
        }
        assert constrained.active_evidence_count == 0
        assert scope_marker not in constrained._rehydrated_runs
        assert run_marker not in constrained._rehydrated_runs
        with pytest.raises(
            ArmEvidenceConflictError,
            match="different LEGACY_CONTROL evidence snapshot",
        ):
            await constrained.start_observation(
                {
                    **opportunity,
                    "legacy_execution_snapshot": replace(
                        legacy,
                        entry_offset_bp=2.0,
                    ),
                },
                [candidate],
            )
        assert await db.fetchone(
            """SELECT COUNT(*) AS n FROM v1469_arm_evidence
            WHERE status = 'PENDING'"""
        ) == {"n": 3}
        payload = await db.fetchone(
            """SELECT canonical_payload_json
            FROM v1469_arm_evidence_profile_payloads"""
        )
        assert payload is not None
        assert json.loads(payload["canonical_payload_json"]) == (
            legacy.to_payload()
        )

        restarted = V1469PairedShadowRuntime(
            repo,
            max_active_evidence=3,
        )
        restored = await restarted.rehydrate_run(
            environment="MAINNET",
            symbol="BTCUSDC",
            source_run_id="run-paired",
        )
        assert restored == {"groups": 1, "evidence": 3, "invalid": 0}
        assert restarted.active_evidence_count == 3
        assert await db.fetchone(
            "SELECT COUNT(*) AS n FROM v1469_arm_evidence"
        ) == {"n": 3}
        assert await db.fetchone(
            "SELECT COUNT(*) AS n FROM v1469_arm_evidence_profile_payloads"
        ) == {"n": 1}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rehydrate_quarantines_legacy_reference_mismatch(
    tmp_path: Path,
) -> None:
    db, repo = await _repo(tmp_path)
    opportunity, candidate = _opportunity(), _candidate()
    divergent_snapshot = {
        **opportunity["feature_snapshot"],
        "signal_reference_price": 101.0,
    }
    divergent_legacy = replace(
        _legacy_snapshot(),
        reference_price=101.0,
    )
    try:
        # The immutable repository snapshot is 100.  Simulate a stale caller
        # that supplied a self-consistent but non-durable 101 snapshot before
        # process loss; restart must trust the durable opportunity and drop it.
        await repo.insert_observation(opportunity, [candidate])
        constrained = V1469PairedShadowRuntime(
            repo,
            max_active_evidence=2,
        )
        dropped = await constrained.start_observation(
            {
                **opportunity,
                "feature_snapshot": divergent_snapshot,
                "legacy_execution_snapshot": divergent_legacy,
            },
            [candidate],
        )
        assert dropped["capacity_dropped"] == 3

        restarted = V1469PairedShadowRuntime(repo)
        restored = await restarted.rehydrate_run(
            environment="MAINNET",
            symbol="BTCUSDC",
            source_run_id="run-paired",
        )
        assert restored == {"groups": 0, "evidence": 0, "invalid": 3}
        statuses = await db.fetchall(
            """SELECT status, terminal_reason, terminal_payload_json
            FROM v1469_arm_evidence ORDER BY evidence_id"""
        )
        assert len(statuses) == 3
        assert {row["status"] for row in statuses} == {"DROPPED"}
        assert {row["terminal_reason"] for row in statuses} == {
            "REHYDRATE_IDENTITY_INVALID"
        }
        assert all(
            "reference price mismatch" in row["terminal_payload_json"]
            for row in statuses
        )
    finally:
        await db.close()

@pytest.mark.asyncio
async def test_pending_paired_group_rehydrates_and_no_fill_is_zero(
    tmp_path: Path,
) -> None:
    db, repo = await _repo(tmp_path)
    opportunity, candidate = _opportunity(), _candidate()
    try:
        await repo.insert_observation(opportunity, [candidate])
        first_runtime = V1469PairedShadowRuntime(repo)
        await first_runtime.start_observation(
            {
                **opportunity,
                "legacy_execution_snapshot": _legacy_snapshot(),
            },
            [candidate],
        )

        restarted = V1469PairedShadowRuntime(repo)
        restored = await restarted.rehydrate_run(
            environment="MAINNET",
            symbol="BTCUSDC",
            source_run_id="run-paired",
        )
        assert restored == {"groups": 1, "evidence": 3, "invalid": 0}
        samples = restarted.cache_samples("run-paired")
        assert len(samples) == 1
        assert samples[0]["outcome_ttl_s"] == 480

        result = await restarted.advance(
            source_run_id="run-paired",
            agg_trade_rows=[],
            coverage_start_ms=OBSERVED_AT_MS,
            coverage_end_ms=OBSERVED_AT_MS + 480_000,
            coverage_complete=True,
            force_data_failure=False,
            now_ms=OBSERVED_AT_MS + 481_000,
            cost_model=_cost_model(),
        )
        assert result["evidence_terminal"] == 3
        rows = await db.fetchall(
            """SELECT execution_profile_id, outcome, fill_status, reward_net_bp
            FROM v1469_arm_evidence"""
        )
        assert {row["execution_profile_id"] for row in rows} == {
            "LEGACY_CONTROL",
            "PASSIVE_BALANCED",
            "RANGE_SCALP",
        }
        assert all(row["outcome"] == "no_fill" for row in rows)
        assert all(row["fill_status"] == "NO_FILL" for row in rows)
        assert all(row["reward_net_bp"] == 0.0 for row in rows)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_broken_shared_envelope_drops_every_profile_without_ev_bias(
    tmp_path: Path,
) -> None:
    db, repo = await _repo(tmp_path)
    opportunity, candidate = _opportunity(), _candidate()
    try:
        await repo.insert_observation(opportunity, [candidate])
        runtime = V1469PairedShadowRuntime(repo)
        await runtime.start_observation(opportunity, [candidate])
        result = await runtime.advance(
            source_run_id="run-paired",
            agg_trade_rows=[{"a": "bad", "T": "bad", "p": "bad"}],
            coverage_start_ms=OBSERVED_AT_MS,
            coverage_end_ms=OBSERVED_AT_MS + 1_000,
            coverage_complete=False,
            force_data_failure=True,
            now_ms=OBSERVED_AT_MS + 2_000,
            cost_model=_cost_model(),
        )
        assert result["evidence_terminal"] == 2
        rows = await db.fetchall(
            """SELECT status, outcome, data_complete, reward_net_bp
            FROM v1469_arm_evidence"""
        )
        assert all(row["status"] == "DROPPED" for row in rows)
        assert all(row["outcome"] == "data_incomplete" for row in rows)
        assert all(row["data_complete"] == 0 for row in rows)
        assert all(row["reward_net_bp"] is None for row in rows)
    finally:
        await db.close()
