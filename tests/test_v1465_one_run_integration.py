from __future__ import annotations

from dataclasses import asdict, replace
import json
import math
import time

import pytest

from src.gridbot.mainnet.one_run import MainnetOneRunManager
from src.gridbot.storage.database import Database
from src.gridbot.storage.repositories import MainnetRunRepository
from src.gridbot.storage.v1465_w6a_profile_repository import (
    V1465W6AProfileRepository,
    W6ASelector,
)
from src.gridbot.strategy.codex_v1_live import CODEX_V1_VERSION
from tests.test_mainnet_one_run_maker import (
    FakeClient,
    FakeRepo,
    FakeTelegramApp,
)
from tests.test_v1460_one_run_integration import (
    ReadyObservationRuntime,
    _codex,
    _wildcat,
)
from tests.test_v1462_one_run_integration import (
    _ordinary_codex_run,
    _settings,
)


async def _manager(tmp_path):
    db = Database(str(tmp_path / "v1465_one_run.db"))
    await db.initialize()
    profile_repo = V1465W6AProfileRepository(db)
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v1464_auto_promotion_enabled=False,
            mainnet_codex_v1465_w6a_profile_shadow_enabled=True,
            mainnet_codex_v1465_w6a_profile_selector_enabled=True,
            mainnet_codex_v1465_w6a_profile_enforcement_enabled=False,
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
        observation_runtime=ReadyObservationRuntime(),
        w6a_profile_repo=profile_repo,
    )
    manager._dca_enabled = False
    return db, profile_repo, manager


class _FailOnceEvidenceRepository:
    def __init__(self, delegate: V1465W6AProfileRepository) -> None:
        self._delegate = delegate
        self.failed = False

    async def upsert_evidence(self, evidence):
        if not self.failed:
            self.failed = True
            raise RuntimeError("transient profile evidence write failure")
        return await self._delegate.upsert_evidence(evidence)

    def __getattr__(self, name):
        return getattr(self._delegate, name)


class _CaptureFetchallDatabase:
    def __init__(self, delegate: Database) -> None:
        self._delegate = delegate
        self.sql = ""
        self.params = ()

    async def fetchall(self, sql, params=()):
        self.sql = str(sql)
        self.params = tuple(params)
        return await self._delegate.fetchall(sql, params)


def _base_sample(opportunity: int, observed_at_ms: int) -> dict:
    return {
        "event_type": "shadow_sample_started",
        "sample_id": f"base-{opportunity}",
        "opportunity_id": f"legacy-{opportunity}",
        "v1462_opportunity_id": f"durable-{opportunity}",
        "run_id": "cry3mn_v1465_profiles",
        "environment": "mainnet",
        "registry_version": "v1.4.62-lane-registry.1",
        "symbol": "ETHUSDC",
        "lane_code": "W6A",
        "effective_lane": "W6A",
        "effective_side": "LONG",
        "classifier_side": "LONG",
        "side": "LONG",
        "strategy": "S1_BB_RSI",
        "market_state": "UNKNOWN",
        "start_ms": observed_at_ms,
        "entry_price": 100.0,
        "entry_reference_price": 100.0,
        "tp_price": 100.06,
        "sl_price": 99.8,
        "fill_model": "limit_touch",
        "promotion_eligible": False,
        "evidence_evaluator_eligible": True,
        "diagnostic_only": False,
        "outcome_ttl_s": 300,
        "features": {
            "setup_age_sec": 60.0,
            "d30": 2.0,
            "vwap_dist_bp": 1.0,
            "pullback_from_recent_high_bp": 4.0,
            "price_above_or_reclaimed_vwap": 1.0,
        },
        "frozen_execution_plan": {
            "schema": "v1463.frozen-effective-ticket.1",
            "side": "LONG",
            "strategy": "S1_BB_RSI",
            "entry_price": 100.0,
            "tp1_price": 100.06,
            "full_tp_price": 100.06,
            "sl_price": 99.8,
            "entry_offset_bp": 0.0,
            "tp1_bp": 6.0,
            "sl_bp": 20.0,
            "partial_exit_pct": 1.0,
            "planned_notional_usdc": 25.0,
            "entry_ttl_s": 180,
            "outcome_ttl_s": 300,
            "action_parameters": {},
        },
    }


def test_w6a_profile_samples_are_paired_and_have_distinct_geometry(
    tmp_path,
) -> None:
    del tmp_path
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v1465_w6a_profile_shadow_enabled=False,
            mainnet_codex_v1465_w6a_profile_selector_enabled=False,
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
        observation_runtime=ReadyObservationRuntime(),
    )
    manager._v1465_w6a_profile_repo = object()
    manager._settings.mainnet_codex_v1465_w6a_profile_shadow_enabled = True

    samples = manager._v1465_build_w6a_profile_samples(
        _base_sample(1, 1_000)
    )

    assert [row["v1465_profile_id"] for row in samples] == [
        "W6A_BASE",
        "W6A_TIGHT",
        "W6A_PASSIVE",
    ]
    assert {row["v1465_opportunity_id"] for row in samples} == {"durable-1"}
    assert all(row["v1462_opportunity_id"] is None for row in samples)
    assert [row["entry_ttl_s"] for row in samples] == [180, 90, 120]
    assert [row["frozen_execution_plan"]["tp1_bp"] for row in samples] == [
        6.0,
        6.0,
        8.0,
    ]
    assert [row["frozen_execution_plan"]["sl_bp"] for row in samples] == [
        20.0,
        10.0,
        12.0,
    ]
    assert samples[2]["entry_price"] < samples[0]["entry_price"]
    assert all(row["v1465_market_state"] == "reclaim" for row in samples)


@pytest.mark.asyncio
async def test_terminal_profile_evidence_selects_one_shadow_winner(
    tmp_path,
) -> None:
    db, profile_repo, manager = await _manager(tmp_path)
    try:
        now_ms = int(time.time() * 1000)
        profile_ev = {
            "W6A_BASE": 2.0,
            "W6A_TIGHT": 1.5,
            "W6A_PASSIVE": 4.5,
        }
        for opportunity in range(8):
            observed_at_ms = now_ms - (8 - opportunity) * 30_000
            profiles = manager._v1465_build_w6a_profile_samples(
                _base_sample(opportunity, observed_at_ms)
            )
            for sample in profiles:
                sample_id = str(sample["sample_id"])
                manager._codex_v1_shadow_samples[sample_id] = sample
                await manager._log_codex_v1_shadow_outcome(
                    sample_id,
                    sample,
                    {
                        "shadow_outcome": "tp1_first",
                        "filled": True,
                        "data_complete": True,
                        "resolved_at_ms": observed_at_ms + 5_000,
                        "paper_pnl_bp_after_fee": profile_ev[
                            str(sample["v1465_profile_id"])
                        ],
                    },
                )

        selector = W6ASelector(
            environment="MAINNET",
            symbol="ETHUSDC",
            lane_code="W6A",
            market_state="reclaim",
            effective_side="LONG",
            strategy="S1_BB_RSI",
        )
        rows = await profile_repo.list_evidence(
            selector,
            window_start_ms=now_ms - 90 * 60 * 1000,
            as_of_ms=now_ms + 60_000,
            eligible_only=False,
        )
        selection = await profile_repo.get_selection(selector)

        assert len(rows) == 24
        assert {
            (row["opportunity_id"], row["profile_id"]) for row in rows
        } == {
            (f"durable-{opportunity}", profile_id)
            for opportunity in range(8)
            for profile_id in (
                "W6A_BASE",
                "W6A_TIGHT",
                "W6A_PASSIVE",
            )
        }
        assert selection is not None
        assert selection["winner_profile_id"] == "W6A_PASSIVE"
        assert selection["status"] == "SHADOW"
        assert int(selection["expires_at_ms"]) > int(selection["renewed_at_ms"])
        assert manager._v1464_terminal_opportunity_ids == set()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_selector_excludes_stale_hash_and_incomplete_profile_groups(
    tmp_path,
) -> None:
    db, profile_repo, manager = await _manager(tmp_path)
    try:
        now_ms = int(time.time() * 1000)
        for opportunity in range(8):
            observed_at_ms = now_ms - (8 - opportunity) * 30_000
            profiles = list(
                manager._v1465_build_w6a_profile_samples(
                    _base_sample(opportunity, observed_at_ms)
                )
            )
            if opportunity < 3:
                profiles[-1]["v1465_resolved_profile_hash"] = "stale-passive"
                profiles[-1]["v1465_profile_plan_hash"] = "stale-passive"
                profiles[-1]["resolved_profile_hash"] = "stale-passive"
            elif opportunity >= 6:
                profiles[-1]["start_ms"] = (
                    int(profiles[-1]["start_ms"]) + 1_000
                )
            for sample in profiles:
                sample_id = str(sample["sample_id"])
                manager._codex_v1_shadow_samples[sample_id] = sample
                await manager._log_codex_v1_shadow_outcome(
                    sample_id,
                    sample,
                    {
                        "shadow_outcome": "tp1_first",
                        "filled": True,
                        "data_complete": not (
                            3 <= opportunity < 6
                            and sample["v1465_profile_id"] == "W6A_PASSIVE"
                        ),
                        "resolved_at_ms": observed_at_ms + 5_000,
                        "paper_pnl_bp_after_fee": 5.0,
                    },
                )

        selector = W6ASelector(
            environment="MAINNET",
            symbol="ETHUSDC",
            lane_code="W6A",
            market_state="reclaim",
            effective_side="LONG",
            strategy="S1_BB_RSI",
        )
        rows = await profile_repo.list_evidence(
            selector,
            window_start_ms=now_ms - 90 * 60 * 1000,
            as_of_ms=now_ms + 60_000,
            eligible_only=False,
        )

        assert len(rows) == 24
        assert await profile_repo.get_selection(selector) is None

        for opportunity in range(100, 108):
            observed_at_ms = now_ms - (108 - opportunity) * 30_000
            for sample in manager._v1465_build_w6a_profile_samples(
                _base_sample(opportunity, observed_at_ms)
            ):
                sample_id = str(sample["sample_id"])
                manager._codex_v1_shadow_samples[sample_id] = sample
                await manager._log_codex_v1_shadow_outcome(
                    sample_id,
                    sample,
                    {
                        "shadow_outcome": "tp1_first",
                        "filled": True,
                        "data_complete": True,
                        "resolved_at_ms": observed_at_ms + 5_000,
                        "paper_pnl_bp_after_fee": {
                            "W6A_BASE": 2.0,
                            "W6A_TIGHT": 1.5,
                            "W6A_PASSIVE": 4.5,
                        }[str(sample["v1465_profile_id"])],
                    },
                )
        selected = await profile_repo.get_selection(selector)
        assert selected is not None
        exclusions = selected["evidence_snapshot"]["identity_exclusions"]
        assert exclusions["resolved_profile_hash_mismatch"] == 3
        assert exclusions["paired_profile_set_incomplete"] == 3
        assert exclusions["paired_profile_set_not_evaluable"] == 3
        assert exclusions["paired_observation_time_mismatch"] == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_outcome_projection_retries_from_ledger_after_restart(
    tmp_path,
) -> None:
    db = Database(str(tmp_path / "v1465_projection_retry.db"))
    await db.initialize()
    legacy_repo = MainnetRunRepository(db)
    profile_repo = V1465W6AProfileRepository(db)
    failing_repo = _FailOnceEvidenceRepository(profile_repo)
    run_id = "cry3mn_v1465_profiles"
    await legacy_repo.create_run(
        {
            "run_id": run_id,
            "symbol": "ETHUSDC",
            "strategy_label": "S1_BB_RSI",
            "status": "RUNNING",
            "params": {},
        }
    )
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v1464_auto_promotion_enabled=False,
            mainnet_codex_v1465_w6a_profile_shadow_enabled=True,
            mainnet_codex_v1465_w6a_profile_selector_enabled=True,
            mainnet_codex_v1465_w6a_profile_enforcement_enabled=False,
        ),
        FakeClient(),
        legacy_repo,
        FakeTelegramApp(),
        observation_runtime=ReadyObservationRuntime(),
        w6a_profile_repo=failing_repo,
    )
    try:
        now_ms = int(time.time() * 1000)
        sample = manager._v1465_build_w6a_profile_samples(
            _base_sample(91, now_ms - 10_000)
        )[0]
        sample_id = str(sample["sample_id"])
        manager._codex_v1_shadow_samples[sample_id] = sample

        await manager._log_codex_v1_shadow_outcome(
            sample_id,
            sample,
            {
                "shadow_outcome": "tp1_first",
                "filled": True,
                "data_complete": True,
                "resolved_at_ms": now_ms - 5_000,
                "paper_pnl_bp_after_fee": 2.0,
            },
        )

        selector = W6ASelector(
            environment="MAINNET",
            symbol="ETHUSDC",
            lane_code="W6A",
            market_state="reclaim",
            effective_side="LONG",
            strategy="S1_BB_RSI",
        )
        assert failing_repo.failed is True
        assert (
            await profile_repo.list_evidence(
                selector,
                window_start_ms=0,
                as_of_ms=now_ms + 60_000,
                eligible_only=False,
            )
            == []
        )
        assert len(
            await legacy_repo.get_unacked_v1465_w6a_profile_outcomes()
        ) == 1

        restarted = MainnetOneRunManager(
            _settings(
                mainnet_codex_v1464_auto_promotion_enabled=False,
                mainnet_codex_v1465_w6a_profile_shadow_enabled=True,
                mainnet_codex_v1465_w6a_profile_selector_enabled=True,
                mainnet_codex_v1465_w6a_profile_enforcement_enabled=False,
            ),
            FakeClient(),
            legacy_repo,
            FakeTelegramApp(),
            observation_runtime=ReadyObservationRuntime(),
            w6a_profile_repo=profile_repo,
        )
        await restarted._reconcile_v1465_w6a_profile_ledger()

        rows = await profile_repo.list_evidence(
            selector,
            window_start_ms=0,
            as_of_ms=now_ms + 60_000,
            eligible_only=False,
        )
        assert len(rows) == 1
        assert rows[0]["opportunity_id"] == "durable-91"
        assert await legacy_repo.get_unacked_v1465_w6a_profile_outcomes() == []
        assert restarted._v1465_profile_ledger_unsafe is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_recompute_retries_after_evidence_commit_and_restart(
    tmp_path,
) -> None:
    db = Database(str(tmp_path / "v1465_recompute_retry.db"))
    await db.initialize()
    legacy_repo = MainnetRunRepository(db)
    profile_repo = V1465W6AProfileRepository(db)
    run_id = "cry3mn_v1465_profiles"
    await legacy_repo.create_run(
        {
            "run_id": run_id,
            "symbol": "ETHUSDC",
            "strategy_label": "S1_BB_RSI",
            "status": "RUNNING",
            "params": {},
        }
    )
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v1464_auto_promotion_enabled=False,
            mainnet_codex_v1465_w6a_profile_shadow_enabled=True,
            mainnet_codex_v1465_w6a_profile_selector_enabled=True,
            mainnet_codex_v1465_w6a_profile_enforcement_enabled=False,
        ),
        FakeClient(),
        legacy_repo,
        FakeTelegramApp(),
        observation_runtime=ReadyObservationRuntime(),
        w6a_profile_repo=profile_repo,
    )
    original_recompute = manager._v1465_recompute_w6a_selection
    recompute_attempts = 0

    async def fail_first_recompute(*args, **kwargs):
        nonlocal recompute_attempts
        recompute_attempts += 1
        if recompute_attempts == 1:
            raise RuntimeError("crash after immutable evidence commit")
        return await original_recompute(*args, **kwargs)

    manager._v1465_recompute_w6a_selection = fail_first_recompute
    try:
        now_ms = int(time.time() * 1000)
        sample = manager._v1465_build_w6a_profile_samples(
            _base_sample(93, now_ms - 10_000)
        )[0]
        sample_id = str(sample["sample_id"])
        manager._codex_v1_shadow_samples[sample_id] = sample
        await manager._log_codex_v1_shadow_outcome(
            sample_id,
            sample,
            {
                "shadow_outcome": "sl_first",
                "filled": True,
                "data_complete": True,
                "resolved_at_ms": now_ms - 5_000,
                "paper_pnl_bp_after_fee": -8.0,
            },
        )

        selector = W6ASelector(
            environment="MAINNET",
            symbol="ETHUSDC",
            lane_code="W6A",
            market_state="reclaim",
            effective_side="LONG",
            strategy="S1_BB_RSI",
        )
        rows = await profile_repo.list_evidence(
            selector,
            window_start_ms=0,
            as_of_ms=now_ms + 60_000,
            eligible_only=False,
        )
        assert len(rows) == 1
        assert len(
            await legacy_repo.get_unacked_v1465_w6a_profile_outcomes()
        ) == 1
        assert manager._v1465_profile_ledger_unsafe is True

        restarted = MainnetOneRunManager(
            _settings(
                mainnet_codex_v1464_auto_promotion_enabled=False,
                mainnet_codex_v1465_w6a_profile_shadow_enabled=True,
                mainnet_codex_v1465_w6a_profile_selector_enabled=True,
                mainnet_codex_v1465_w6a_profile_enforcement_enabled=True,
            ),
            FakeClient(),
            legacy_repo,
            FakeTelegramApp(),
            observation_runtime=ReadyObservationRuntime(),
            w6a_profile_repo=profile_repo,
        )
        restarted_recomputes = 0
        restarted_original = restarted._v1465_recompute_w6a_selection

        async def count_recompute(*args, **kwargs):
            nonlocal restarted_recomputes
            restarted_recomputes += 1
            return await restarted_original(*args, **kwargs)

        restarted._v1465_recompute_w6a_selection = count_recompute
        assert restarted._v1465_profile_ledger_unsafe is True
        await restarted._reconcile_v1465_w6a_profile_ledger()

        assert restarted_recomputes == 1
        assert await legacy_repo.get_unacked_v1465_w6a_profile_outcomes() == []
        assert restarted._v1465_profile_ledger_unsafe is False
        replayed = await profile_repo.list_evidence(
            selector,
            window_start_ms=0,
            as_of_ms=now_ms + 60_000,
            eligible_only=False,
        )
        assert len(replayed) == 1
        assert replayed[0]["evidence_hash"] == rows[0]["evidence_hash"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_partial_profile_starts_are_repaired_idempotently_from_base_ledger(
    tmp_path,
) -> None:
    db = Database(str(tmp_path / "v1465_start_retry.db"))
    await db.initialize()
    legacy_repo = MainnetRunRepository(db)
    profile_repo = V1465W6AProfileRepository(db)
    run_id = "cry3mn_v1465_profiles"
    await legacy_repo.create_run(
        {
            "run_id": run_id,
            "symbol": "ETHUSDC",
            "strategy_label": "S1_BB_RSI",
            "status": "RUNNING",
            "params": {},
        }
    )
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v1464_auto_promotion_enabled=False,
            mainnet_codex_v1465_w6a_profile_shadow_enabled=True,
            mainnet_codex_v1465_w6a_profile_selector_enabled=True,
            mainnet_codex_v1465_w6a_profile_enforcement_enabled=False,
        ),
        FakeClient(),
        legacy_repo,
        FakeTelegramApp(),
        observation_runtime=ReadyObservationRuntime(),
        w6a_profile_repo=profile_repo,
    )
    try:
        base = {
            **_base_sample(92, int(time.time() * 1000) - 10_000),
            "version": CODEX_V1_VERSION,
        }
        profiles = manager._v1465_build_w6a_profile_samples(base)
        await legacy_repo.log_event(
            run_id,
            "entry_codex_v1_shadow_sample_started",
            base,
        )
        await legacy_repo.log_event(
            run_id,
            "entry_codex_v1_shadow_sample_started",
            profiles[0],
        )
        assert len(
            await legacy_repo.get_incomplete_v1465_w6a_profile_start_groups(
                version=CODEX_V1_VERSION
            )
        ) == 1

        await manager._reconcile_v1465_w6a_profile_ledger()
        events = await legacy_repo.get_v1465_w6a_profile_events(
            run_id,
            "durable-92",
        )
        started = [
            json.loads(row["details_json"])
            for row in events
            if row["event_type"]
            == "entry_codex_v1_shadow_sample_started"
        ]
        assert {
            row["v1465_profile_id"]
            for row in started
            if row.get("v1465_profile_evidence") is True
        } == {"W6A_BASE", "W6A_TIGHT", "W6A_PASSIVE"}
        first_event_count = len(events)

        await manager._reconcile_v1465_w6a_profile_ledger()
        assert len(
            await legacy_repo.get_v1465_w6a_profile_events(
                run_id,
                "durable-92",
            )
        ) == first_event_count
        assert (
            await legacy_repo.get_incomplete_v1465_w6a_profile_start_groups(
                version=CODEX_V1_VERSION
            )
            == []
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_ledger_reconciliation_queries_use_v1465_indexes(
    tmp_path,
) -> None:
    db = Database(str(tmp_path / "v1465_ledger_query_plan.db"))
    await db.initialize()
    capture = _CaptureFetchallDatabase(db)
    repo = MainnetRunRepository(capture)
    try:
        index_rows = await db.fetchall(
            """SELECT name
            FROM sqlite_master
            WHERE type = 'index'
              AND name LIKE 'idx_v1465_w6a_%'"""
        )
        assert {
            row["name"] for row in index_rows
        } >= {
            "idx_v1465_w6a_base_start_intent",
            "idx_v1465_w6a_profile_start_group",
            "idx_v1465_w6a_terminal_outcome",
            "idx_v1465_w6a_projection_ack",
        }

        await repo.get_incomplete_v1465_w6a_profile_start_groups(
            version=CODEX_V1_VERSION,
            limit=500,
        )
        base_plan = await db.fetchall(
            f"EXPLAIN QUERY PLAN {capture.sql}",
            capture.params,
        )
        base_details = "\n".join(
            str(row["detail"]) for row in base_plan
        )
        assert "idx_v1465_w6a_base_start_intent" in base_details
        assert "idx_v1465_w6a_profile_start_group" in base_details

        await repo.get_unacked_v1465_w6a_profile_outcomes(limit=500)
        outcome_plan = await db.fetchall(
            f"EXPLAIN QUERY PLAN {capture.sql}",
            capture.params,
        )
        outcome_details = "\n".join(
            str(row["detail"]) for row in outcome_plan
        )
        assert "idx_v1465_w6a_terminal_outcome" in outcome_details
        assert "idx_v1465_w6a_projection_ack" in outcome_details
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unchanged_evidence_can_renew_same_winner_more_than_once(
    tmp_path,
) -> None:
    db, profile_repo, manager = await _manager(tmp_path)
    try:
        now_ms = int(time.time() * 1000)
        for opportunity in range(8):
            observed_at_ms = now_ms - (8 - opportunity) * 30_000
            for sample in manager._v1465_build_w6a_profile_samples(
                _base_sample(opportunity, observed_at_ms)
            ):
                sample_id = str(sample["sample_id"])
                manager._codex_v1_shadow_samples[sample_id] = sample
                await manager._log_codex_v1_shadow_outcome(
                    sample_id,
                    sample,
                    {
                        "shadow_outcome": "tp1_first",
                        "filled": True,
                        "data_complete": True,
                        "resolved_at_ms": observed_at_ms + 5_000,
                        "paper_pnl_bp_after_fee": {
                            "W6A_BASE": 2.0,
                            "W6A_TIGHT": 1.5,
                            "W6A_PASSIVE": 4.5,
                        }[str(sample["v1465_profile_id"])],
                    },
                )
        selector = W6ASelector(
            environment="MAINNET",
            symbol="ETHUSDC",
            lane_code="W6A",
            market_state="reclaim",
            effective_side="LONG",
            strategy="S1_BB_RSI",
        )
        initial = await profile_repo.get_selection(selector)
        assert initial is not None
        first_renewal_ms = int(initial["renewed_at_ms"]) + 1_000

        await manager._v1465_recompute_w6a_selection(
            selector,
            now_ms=first_renewal_ms,
            source_run_id="renew-1",
        )
        first = await profile_repo.get_selection(selector)
        await manager._v1465_recompute_w6a_selection(
            selector,
            now_ms=first_renewal_ms + 1_000,
            source_run_id="renew-2",
        )
        second = await profile_repo.get_selection(selector)
        events = await profile_repo.list_selection_events(
            selector_key=selector.key
        )

        assert first is not None and second is not None
        assert first["winner_profile_id"] == second["winner_profile_id"]
        assert int(first["generation"]) == int(initial["generation"]) + 1
        assert int(second["generation"]) == int(first["generation"]) + 1
        assert int(second["expires_at_ms"]) > int(first["expires_at_ms"])
        assert [row["event_type"] for row in events][-2:] == [
            "RENEWED",
            "RENEWED",
        ]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_live_w6a_selection_applies_profile_and_revalidates_generation(
    tmp_path,
) -> None:
    db, profile_repo, manager = await _manager(tmp_path)
    try:
        manager._settings.mainnet_codex_v1465_w6a_profile_enforcement_enabled = (
            True
        )
        now_ms = int(time.time() * 1000)
        selector = W6ASelector(
            environment="MAINNET",
            symbol="ETHUSDC",
            lane_code="W6A",
            market_state="reclaim",
            effective_side="LONG",
            strategy="S1_BB_RSI",
        )
        profile_id = "W6A_TIGHT"
        profile_hash = manager._v1465_w6a_profile_hash(profile_id)
        selection = {
            **asdict(selector),
            "winner_profile_id": profile_id,
            "winner_resolved_profile_hash": profile_hash,
            "status": "LIVE",
            "notional_cap_usdc": 25.0,
            "issued_at_ms": now_ms,
            "renewed_at_ms": now_ms,
            "expires_at_ms": now_ms + 600_000,
            "evidence_revision": "revision-1",
            "evidence_snapshot": {"winner": profile_id},
            "policy_hash": manager._v1465_w6a_selector_policy_hash(),
            "owner_id": "test-owner",
            "boot_id": "test-boot",
            "demotion_reason": None,
            "demoted_at_ms": None,
            "cooldown_until_ms": None,
        }
        await profile_repo.grant_selection(
            selection,
            expected_generation=0,
            event_time_ms=now_ms,
            idempotency_key="grant-live-w6a",
            actor="test",
        )
        features = {
            "setup_age_sec": 60.0,
            "d30": 2.0,
            "vwap_dist_bp": 1.0,
            "pullback_from_recent_high_bp": 4.0,
            "price_above_or_reclaimed_vwap": 1.0,
        }
        raw = replace(
            _codex(market_state="UNKNOWN", lane_code="W6A"),
            side="LONG",
            strategy="S1_BB_RSI",
            entry_offset_bp=0.0,
        )
        run = _ordinary_codex_run("cry3mn_v1465_live_selection")
        v1460 = await manager._v1460_apply_lane_policy(run, raw)

        admitted = await manager._v1462_apply_strict_admission(
            run,
            raw,
            True,
            raw,
            v1460,
            wildcat_decision=_wildcat(),
            features=features,
        )

        assert admitted.accepted is True, (
            admitted.metrics.get("v1465_w6a_profile_selection", {}).get(
                "reason"
            ),
            admitted.metrics.get("v1465_w6a_profile_selection", {}).get(
                "selector_key"
            ),
            selector.key,
        )
        assert admitted.reason == "v1465.w6a_profile_lease_authorized"
        assert admitted.entry_offset_bp == 0.0
        assert admitted.metrics["tp1_bp"] == 6.0
        assert admitted.metrics["sl_bp"] == 10.0
        assert admitted.metrics["ttl_s"] == 90
        assert admitted.metrics["partial_exit_pct"] == 1.0
        assert admitted.metrics["applied_notional_cap_usdc"] == 25.0
        assert "v1464_adaptive_promotion" not in admitted.metrics
        selection_meta = admitted.metrics["v1465_w6a_profile_selection"]
        selected_plan = selection_meta["selected_execution_plan"]
        assert selected_plan["entry_offset_bp"] == 0.0
        assert selected_plan["tp1_bp"] == 6.0
        assert math.isclose(selected_plan["sl_bp"], 10.0, abs_tol=1e-9)
        assert selected_plan["entry_ttl_s"] == 90
        assert selected_plan["partial_exit_pct"] == 1.0
        assert (
            admitted.metrics["v1462_admission"]["frozen_execution_plan"]
            == selected_plan
        )
        paid = manager._apply_codex_v1_decision(_wildcat(), admitted)
        paid_entry = float(paid.signal.entries[0])
        paid_stop = float(paid.signal.stop_loss)
        assert math.isclose(
            abs(paid_entry - paid_stop) / paid_entry * 10_000.0,
            10.0,
            abs_tol=1e-9,
        )

        manager._v1465_profile_ledger_unsafe = True
        claimed, reason = await manager._v1465_claim_w6a_entry_authority(
            admitted,
            actual_decision=paid,
            actual_notional_usdc=25.0,
        )
        assert claimed is None
        assert reason == "v1465_profile_ledger_unsafe"
        manager._v1465_profile_ledger_unsafe = False

        claimed, reason = await manager._v1465_claim_w6a_entry_authority(
            admitted,
            actual_decision=paid,
            actual_notional_usdc=25.0,
        )
        assert claimed is admitted
        assert reason == "v1465_selection_revalidated"

        tampered_decisions = (
            replace(paid, side="SHORT"),
            replace(
                paid,
                signal=replace(paid.signal, stop_loss=paid_stop - 0.01),
            ),
            replace(
                paid,
                signal=replace(
                    paid.signal,
                    take_profits=[
                        float(paid.signal.take_profits[0]) + 0.01
                    ],
                ),
            ),
        )
        for tampered in tampered_decisions:
            claimed, reason = await manager._v1465_claim_w6a_entry_authority(
                admitted,
                actual_decision=tampered,
                actual_notional_usdc=25.0,
            )
            assert claimed is None
            assert reason == "v1465_actual_execution_plan_changed"

        current = await profile_repo.get_selection(selector)
        assert current is not None
        current = dict(current)
        current.pop("evidence_snapshot_hash", None)
        await profile_repo.renew_selection(
            {
                **current,
                "renewed_at_ms": now_ms + 1,
                "expires_at_ms": now_ms + 600_001,
                "evidence_snapshot": {"winner": profile_id, "new": True},
            },
            expected_generation=int(current["generation"]),
            event_time_ms=now_ms + 1,
            idempotency_key="renew-live-w6a",
            actor="test",
        )
        claimed, reason = await manager._v1465_claim_w6a_entry_authority(
            admitted,
            actual_decision=paid,
            actual_notional_usdc=25.0,
        )
        assert claimed is None
        assert reason == "v1465_generation_changed"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_enforcement_transition_recomputes_fresh_shadow_without_new_outcome(
    tmp_path,
) -> None:
    db, profile_repo, manager = await _manager(tmp_path)
    try:
        now_ms = int(time.time() * 1000)
        for opportunity in range(8):
            observed_at_ms = now_ms - (8 - opportunity) * 30_000
            for sample in manager._v1465_build_w6a_profile_samples(
                _base_sample(opportunity, observed_at_ms)
            ):
                sample_id = str(sample["sample_id"])
                manager._codex_v1_shadow_samples[sample_id] = sample
                ev = {
                    "W6A_BASE": 2.0,
                    "W6A_TIGHT": 1.5,
                    "W6A_PASSIVE": 4.5,
                }[str(sample["v1465_profile_id"])]
                await manager._log_codex_v1_shadow_outcome(
                    sample_id,
                    sample,
                    {
                        "shadow_outcome": "tp1_first",
                        "filled": True,
                        "data_complete": True,
                        "resolved_at_ms": observed_at_ms + 5_000,
                        "paper_pnl_bp_after_fee": ev,
                    },
                )
        selector = W6ASelector(
            environment="MAINNET",
            symbol="ETHUSDC",
            lane_code="W6A",
            market_state="reclaim",
            effective_side="LONG",
            strategy="S1_BB_RSI",
        )
        shadow = await profile_repo.get_selection(selector)
        assert shadow is not None and shadow["status"] == "SHADOW"
        assert shadow["winner_profile_id"] == "W6A_PASSIVE"

        manager._settings.mainnet_codex_v1465_w6a_profile_enforcement_enabled = (
            True
        )
        features = dict(_base_sample(99, now_ms)["features"])
        raw = replace(
            _codex(market_state="UNKNOWN", lane_code="W6A"),
            side="LONG",
            strategy="S1_BB_RSI",
            entry_offset_bp=0.0,
        )
        run = _ordinary_codex_run("cry3mn_v1465_enable_transition")
        v1460 = await manager._v1460_apply_lane_policy(run, raw)
        admitted = await manager._v1462_apply_strict_admission(
            run,
            raw,
            True,
            raw,
            v1460,
            wildcat_decision=_wildcat(),
            features=features,
        )

        assert admitted.accepted is True
        assert (
            admitted.metrics["v1465_w6a_profile_selection"][
                "winner_profile_id"
            ]
            == "W6A_PASSIVE"
        )
        live = await profile_repo.get_selection(selector)
        assert live is not None and live["status"] == "LIVE"
        assert int(live["generation"]) > int(shadow["generation"])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_expired_shadow_without_fresh_evidence_cannot_become_live(
    tmp_path,
) -> None:
    db, profile_repo, manager = await _manager(tmp_path)
    try:
        now_ms = int(time.time() * 1000)
        selector = W6ASelector(
            environment="MAINNET",
            symbol="ETHUSDC",
            lane_code="W6A",
            market_state="reclaim",
            effective_side="LONG",
            strategy="S1_BB_RSI",
        )
        profile_id = "W6A_BASE"
        await profile_repo.grant_selection(
            {
                **asdict(selector),
                "winner_profile_id": profile_id,
                "winner_resolved_profile_hash": (
                    manager._v1465_w6a_profile_hash(profile_id)
                ),
                "status": "SHADOW",
                "notional_cap_usdc": 0.0,
                "issued_at_ms": now_ms - 1_200_000,
                "renewed_at_ms": now_ms - 1_200_000,
                "expires_at_ms": now_ms - 600_000,
                "evidence_revision": "expired-revision",
                "evidence_snapshot": {},
                "policy_hash": manager._v1465_w6a_selector_policy_hash(),
                "owner_id": "test",
                "boot_id": "test",
                "demotion_reason": None,
                "demoted_at_ms": None,
                "cooldown_until_ms": None,
            },
            expected_generation=0,
            event_time_ms=now_ms - 1_200_000,
            idempotency_key="expired-shadow",
            actor="test",
        )
        manager._settings.mainnet_codex_v1465_w6a_profile_enforcement_enabled = (
            True
        )
        raw = replace(
            _codex(market_state="UNKNOWN", lane_code="W6A"),
            side="LONG",
            strategy="S1_BB_RSI",
            entry_offset_bp=0.0,
        )
        run = _ordinary_codex_run("cry3mn_v1465_expired_transition")
        v1460 = await manager._v1460_apply_lane_policy(run, raw)
        admitted = await manager._v1462_apply_strict_admission(
            run,
            raw,
            True,
            raw,
            v1460,
            wildcat_decision=_wildcat(),
            features=dict(_base_sample(99, now_ms)["features"]),
        )

        assert admitted.accepted is False
        assert admitted.reason == "v1462.shadow.rule_not_allowlisted"
        stored = await profile_repo.get_selection(selector)
        assert stored is not None
        assert stored["status"] == "DEMOTED"
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pre_gate_accepted", "reject_lineage"),
    ((False, ()), (True, ("legacy_reject_reopen",))),
)
async def test_v1465_never_reopens_upstream_rejects(
    tmp_path,
    pre_gate_accepted,
    reject_lineage,
) -> None:
    db, profile_repo, manager = await _manager(tmp_path)
    del profile_repo
    try:
        manager._settings.mainnet_codex_v1465_w6a_profile_enforcement_enabled = (
            True
        )
        raw = replace(
            _codex(market_state="UNKNOWN", lane_code="W6A"),
            side="LONG",
            strategy="S1_BB_RSI",
            entry_offset_bp=0.0,
        )
        run = _ordinary_codex_run("cry3mn_v1465_reject_lineage")
        v1460 = await manager._v1460_apply_lane_policy(run, raw)
        admitted = await manager._v1462_apply_strict_admission(
            run,
            raw,
            pre_gate_accepted,
            raw,
            v1460,
            wildcat_decision=_wildcat(),
            reject_lineage=reject_lineage,
            features=dict(_base_sample(99, int(time.time() * 1000))["features"]),
        )

        assert admitted.accepted is False
        assert admitted.requested_notional_usdc == 0.0
        assert admitted.reason != "v1465.w6a_profile_lease_authorized"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v1465_selector_ownership_blocks_v1464_w6a_bypass(
    tmp_path,
) -> None:
    class ExplodingV1464Runtime:
        @property
        def config(self):
            raise AssertionError("v1.4.64 must not own W6A")

        async def evaluate_candidate(self, **kwargs):
            del kwargs
            raise AssertionError("v1.4.64 must not evaluate W6A")

    db, profile_repo, manager = await _manager(tmp_path)
    del profile_repo
    try:
        manager._settings.mainnet_codex_v1464_auto_promotion_enabled = True
        manager._settings.mainnet_codex_v1465_w6a_profile_selector_enabled = True
        manager._settings.mainnet_codex_v1465_w6a_profile_enforcement_enabled = (
            False
        )
        manager._v1464_promotion_runtime = ExplodingV1464Runtime()
        raw = replace(
            _codex(market_state="UNKNOWN", lane_code="W6A"),
            side="LONG",
            strategy="S1_BB_RSI",
            entry_offset_bp=0.0,
        )
        run = _ordinary_codex_run("cry3mn_v1465_owns_w6a")
        v1460 = await manager._v1460_apply_lane_policy(run, raw)
        admitted = await manager._v1462_apply_strict_admission(
            run,
            raw,
            True,
            raw,
            v1460,
            wildcat_decision=_wildcat(),
            features=dict(_base_sample(99, int(time.time() * 1000))["features"]),
        )

        assert admitted.accepted is False
        assert "v1464_adaptive_promotion" not in admitted.metrics
    finally:
        await db.close()
