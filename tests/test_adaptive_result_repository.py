import json
from hashlib import sha256
import sqlite3

import pytest

from src.gridbot.storage.adaptive_evidence_repository import AdaptiveEvidenceRepository
from src.gridbot.storage.adaptive_result_repository import AdaptiveResultRepository
from src.gridbot.storage.database import Database


def _session() -> dict:
    return {
        "session_id": "session-a",
        "environment": "mainnet",
        "account_fingerprint": "account-a",
        "database_identity": "mainnet.db",
        "exchange_endpoint": "https://fapi.binance.com",
        "is_testnet": False,
        "symbol": "ETHUSDC",
        "account_mode": "ONE_WAY",
        "deployment_commit": "commit-a",
        "code_version": "v1.4.59",
        "config_sha256": "config-a",
        "status": "STOPPED",
        "started_at_ms": 1_000,
    }


def _feature_hash(features: dict) -> str:
    encoded = json.dumps(
        features,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _opportunity(opportunity_id: str = "opp-a") -> dict:
    features = {"opportunity_id": opportunity_id, "rng15_bp": 20.0}
    return {
        "session_id": "session-a",
        "opportunity_id": opportunity_id,
        "observed_at_ms": 1_010,
        "decision_at_ms": 1_009,
        "source_run_id": "run-a",
        "opportunity_bucket": 1,
        "feature_hash": _feature_hash(features),
        "feature_snapshot": features,
        "feature_timestamps": {"rng15_bp": 1_008},
        "evidence_contract_version": "v1459-opportunity-evidence-v2",
        "outcome_blind": True,
        "symbol": "ETHUSDC",
        "side": "BUY",
        "lane_code": "STUP-S",
        "market_state": "STUP-S:clean_extension",
        "decision_schema_version": "v1",
        "action_schema": {"tp_bp": 14},
        "raw_decision": {"accepted": True},
        "effective_decision": {"route": "NORMAL"},
    }


def _evaluation(opportunity_id: str = "opp-a", **overrides) -> dict:
    value = {
        "session_id": "session-a",
        "opportunity_id": opportunity_id,
        "variant": "E2",
        "fill_model": "TRADE_THROUGH",
        "simulation_version": "v1.4.59",
        "entry_offset_bp": 2.0,
        "entry_limit_price": 100.0,
        "decision_latency_ms": 2,
        "entry_ttl_ms": 90_000,
        "fill_status": "FILLED",
        "filled_qty": 1.0,
        "avg_fill_price": 100.0,
        "first_fill_at_ms": 1_020,
        "fill_age_ms": 10,
        "partial_fill_ratio": 1.0,
        "tp_anchor": "ENTRY",
        "tp_bp": 14.0,
        "sl_anchor": "ENTRY",
        "sl_bp": 8.0,
        "max_hold_ms": 300_000,
        "mfe_bp": 15.0,
        "mae_bp": 2.0,
        "exit_at_ms": 2_000,
        "exit_price": 100.14,
        "exit_reason": "TP",
        "gross_pnl_usdc": 0.14,
        "commission_usdc": 0.01,
        "funding_usdc": 0.0,
        "net_pnl_usdc": 0.13,
        "data_quality": "COMPLETE",
        "ambiguous_touch": False,
        "input": {"fees": {"entry": 0.0}},
        "recorded_at_ms": 3_000,
    }
    value.update(overrides)
    return value


def _reconciliation(run_id: str, **overrides) -> dict:
    value = {
        "run_id": run_id,
        "reconciliation_revision": 0,
        "environment": "mainnet",
        "account_fingerprint": "account-a",
        "symbol": "ETHUSDC",
        "reconciliation_status": "COMPLETE",
        "gross_realized_pnl_usdc": 0.2,
        "commission_usdc": 0.01,
        "funding_usdc": 0.02,
        "net_pnl_usdc": 0.17,
        "entry_maker_fills": 1,
        "exit_taker_fills": 1,
        "source": {"exchange": "mainnet"},
        "reconciled_at_ms": 4_000,
    }
    value.update(overrides)
    return value


def _trade(trade_id: str = "trade-a") -> dict:
    return {
        "exchange_trade_id": trade_id,
        "order_id": "order-a",
        "role": "ENTRY",
        "is_maker": True,
        "realized_pnl_usdc": 0.0,
        "commission_amount": 0.01,
        "commission_asset": "USDC",
        "commission_usdc": 0.01,
        "source": {"trade": trade_id},
    }


def _income(income_id: str = "income-a") -> dict:
    return {
        "exchange_income_id": income_id,
        "income_type": "FUNDING_FEE",
        "amount": -0.02,
        "asset": "USDC",
        "amount_usdc": -0.02,
        "source": {"income": income_id},
    }


async def _repositories(tmp_path):
    db = Database(str(tmp_path / "adaptive-results.db"))
    await db.initialize()
    evidence = AdaptiveEvidenceRepository(db)
    assert await evidence.upsert_session(_session())
    assert await evidence.record_opportunity(_opportunity())
    assert await evidence.record_opportunity(_opportunity("opp-b"))
    return db, AdaptiveResultRepository(db)


async def _seed_run(db: Database, run_id: str) -> None:
    await db.execute(
        """INSERT INTO mainnet_runs
        (run_id, symbol, strategy_label, status, armed_at_ms, updated_at_ms)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (run_id, "ETHUSDC", "test", "COMPLETED", 1_000, 1_000),
    )


@pytest.mark.asyncio
async def test_shadow_evaluation_is_immutable_and_complete_filter_is_deterministic(tmp_path):
    db, repo = await _repositories(tmp_path)
    try:
        assert await repo.record_shadow_evaluation(_evaluation())
        assert await repo.record_shadow_evaluation(_evaluation()) is False
        with pytest.raises(ValueError, match="conflicting immutable shadow evaluation"):
            await repo.record_shadow_evaluation(_evaluation(net_pnl_usdc=0.12))
        stored = await repo.get_shadow_evaluation("session-a", "opp-a", "E2", "TRADE_THROUGH", "v1.4.59")
        assert stored["net_pnl_usdc"] == pytest.approx(0.13)
        assert stored["input_json"] == '{"fees":{"entry":0.0}}'

        assert await repo.record_shadow_evaluation(
            _evaluation(
                opportunity_id="opp-b", variant="E0", data_quality="DATA_INCOMPLETE",
                avg_fill_price=None, first_fill_at_ms=None, fill_age_ms=None, exit_at_ms=None,
                exit_price=None, exit_reason=None, gross_pnl_usdc=None, commission_usdc=None,
                funding_usdc=None, net_pnl_usdc=None, recorded_at_ms=2_000,
            )
        )
        complete = await repo.list_shadow_evaluations("session-a", complete_only=True)
        assert [row["opportunity_id"] for row in complete] == ["opp-a"]
        all_rows = await repo.list_shadow_evaluations("session-a")
        assert [row["opportunity_id"] for row in all_rows] == ["opp-b", "opp-a"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reconciliation_transaction_is_idempotent_and_complete_filter_excludes_incomplete(tmp_path):
    db, repo = await _repositories(tmp_path)
    try:
        await _seed_run(db, "run-a")
        assert await repo.record_reconciliation(
            _reconciliation("run-a"), trades=[_trade()], incomes=[_income()]
        )
        assert await repo.record_reconciliation(
            _reconciliation("run-a"), trades=[_trade()], incomes=[_income()]
        ) is False
        with pytest.raises(ValueError, match="conflicting reconciliation revision"):
            await repo.record_reconciliation(
                _reconciliation("run-a", net_pnl_usdc=0.16), trades=[_trade()], incomes=[_income()]
            )
        assert len(await repo.get_reconciliation_trades("run-a", 0)) == 1
        assert len(await repo.get_reconciliation_incomes("run-a", 0)) == 1

        await _seed_run(db, "run-incomplete")
        assert await repo.record_reconciliation(
            _reconciliation(
                "run-incomplete", reconciliation_status="DATA_INCOMPLETE", completeness_reason="MISSING_ID",
                commission_usdc=None, funding_usdc=None, net_pnl_usdc=None, reconciled_at_ms=3_000,
            )
        )
        complete = await repo.list_reconciliations(
            environment="mainnet", account_fingerprint="account-a", symbol="ETHUSDC", complete_only=True
        )
        assert [row["run_id"] for row in complete] == ["run-a"]
        all_rows = await repo.list_reconciliations(
            environment="mainnet", account_fingerprint="account-a", symbol="ETHUSDC"
        )
        assert [row["run_id"] for row in all_rows] == ["run-incomplete", "run-a"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reconciliation_revision_inherits_prior_exchange_evidence(tmp_path):
    db, repo = await _repositories(tmp_path)
    try:
        await _seed_run(db, "run-revisioned")
        assert await repo.record_reconciliation(
            _reconciliation("run-revisioned"),
            trades=[_trade("entry-trade")],
            incomes=[_income("funding-a")],
        )
        assert await repo.record_reconciliation(
            _reconciliation(
                "run-revisioned",
                reconciliation_revision=1,
                reconciled_at_ms=5_000,
            ),
            trades=[_trade("exit-trade")],
            incomes=[_income("funding-b")],
        )

        trades = await repo.get_reconciliation_trades("run-revisioned", 1)
        incomes = await repo.get_reconciliation_incomes("run-revisioned", 1)
        assert [row["exchange_trade_id"] for row in trades] == ["entry-trade", "exit-trade"]
        assert [row["exchange_income_id"] for row in incomes] == ["funding-a", "funding-b"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_global_exchange_id_conflict_rolls_back_parent_and_all_children(tmp_path):
    db, repo = await _repositories(tmp_path)
    try:
        await _seed_run(db, "run-a")
        await _seed_run(db, "run-b")
        assert await repo.record_reconciliation(
            _reconciliation("run-a"), trades=[_trade("shared-trade")], incomes=[_income("income-a")]
        )
        with pytest.raises(sqlite3.IntegrityError):
            await repo.record_reconciliation(
                _reconciliation("run-b"),
                trades=[_trade("shared-trade")],
                incomes=[_income("income-b")],
            )
        assert await repo.get_reconciliation("run-b", 0) is None
        assert await repo.get_reconciliation_incomes("run-b", 0) == []
        income = await db.fetchone(
            "SELECT * FROM run_reconciliation_exchange_income WHERE exchange_income_id = ?",
            ("income-b",),
        )
        assert income is None
    finally:
        await db.close()
