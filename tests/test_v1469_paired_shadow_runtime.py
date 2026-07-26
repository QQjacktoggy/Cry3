from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.gridbot.mainnet.v1469_arbiter_evidence_mapper import (
    map_durable_paired_evidence,
)
from src.gridbot.mainnet.v1469_paired_evaluator import ShadowCostModel
from src.gridbot.mainnet.v1469_paired_shadow_runtime import (
    V1469PairedShadowRuntime,
)
from src.gridbot.storage.database import Database
from src.gridbot.storage.v1469_arm_observation_repository import (
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


async def _repo(
    tmp_path: Path,
) -> tuple[Database, V1469ArmObservationRepository]:
    db = Database(str(tmp_path / "paired.db"))
    await db.initialize()
    return db, V1469ArmObservationRepository(db)


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
async def test_pending_paired_group_rehydrates_and_no_fill_is_zero(
    tmp_path: Path,
) -> None:
    db, repo = await _repo(tmp_path)
    opportunity, candidate = _opportunity(), _candidate()
    try:
        await repo.insert_observation(opportunity, [candidate])
        first_runtime = V1469PairedShadowRuntime(repo)
        await first_runtime.start_observation(opportunity, [candidate])

        restarted = V1469PairedShadowRuntime(repo)
        restored = await restarted.rehydrate_run(
            environment="MAINNET",
            symbol="BTCUSDC",
            source_run_id="run-paired",
        )
        assert restored == {"groups": 1, "evidence": 2, "invalid": 0}
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
        assert result["evidence_terminal"] == 2
        rows = await db.fetchall(
            """SELECT outcome, fill_status, reward_net_bp
            FROM v1469_arm_evidence"""
        )
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
