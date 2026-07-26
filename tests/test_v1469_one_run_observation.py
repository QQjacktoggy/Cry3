import asyncio
from dataclasses import replace
from types import SimpleNamespace

import src.gridbot.mainnet.one_run as one_run_module
from src.gridbot.mainnet.one_run import MainnetOneRunManager
from src.gridbot.strategy.codex_v1_live import select_codex_v1_lane


def _features() -> dict:
    return {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "LONG",
        "score": 70.0,
        "rng15": 40.0,
        "range_bp": 10.0,
        "feature_age_seconds": 0.0,
    }


class _RecordingRepository:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[dict, tuple[dict, ...]]] = []

    async def insert_observation(
        self,
        opportunity: dict,
        candidates: tuple[dict, ...],
    ) -> None:
        self.calls.append((opportunity, candidates))
        if self.fail:
            raise RuntimeError("simulated observation write failure")


def _manager(repository: _RecordingRepository) -> MainnetOneRunManager:
    manager = object.__new__(MainnetOneRunManager)
    manager._settings = SimpleNamespace(
        mainnet_codex_v1469_observation_enabled=True,
        mainnet_codex_v1469_observation_bucket_seconds=30,
    )
    manager._v1469_arm_observation_repo = repository
    manager._v1469_observed_opportunity_ids = set()
    manager._v1469_observation_inflight_ids = set()
    manager._v1469_observation_tasks = set()
    manager._v1469_observation_dropped = 0
    manager._v1469_observation_repo_missing_logged = False
    manager._v1459_regime_runtimes = {}
    manager._v1469_observation_regime_runtimes = {}
    manager._v1469_observation_active = lambda run: True
    return manager


def test_one_run_records_selected_block_and_overlapping_suppressed_lane(
    monkeypatch,
) -> None:
    repository = _RecordingRepository()
    manager = _manager(repository)
    features = _features()
    selected = select_codex_v1_lane(features)
    blocked = replace(
        selected,
        accepted=False,
        reason="codex_v1_lane_disabled",
        size_mult=0.0,
        notional_mult=0.0,
        requested_notional_usdc=0.0,
    )
    monkeypatch.setattr(one_run_module.time, "time", lambda: 120.001)

    async def exercise() -> None:
        await manager._v1469_record_lane_observation(
            {"run_id": "cry3mn-observe"},
            features,
            selector_decision=selected,
            effective_decision=blocked,
        )
        await manager.shutdown_v1469_observation_writer()
        # A scheduler retry in the same opportunity bucket must not duplicate
        # the normalized observation bundle in this process.
        await manager._v1469_record_lane_observation(
            {"run_id": "cry3mn-observe"},
            features,
            selector_decision=selected,
            effective_decision=blocked,
        )

    asyncio.run(exercise())

    assert len(repository.calls) == 1
    opportunity, candidates = repository.calls[0]
    assert opportunity["source_run_id"] == "cry3mn-observe"
    assert opportunity["data_quality"] == "COMPLETE"
    assert "observation_only" not in opportunity["feature_snapshot"]
    by_lane = {candidate["lane_code"]: candidate for candidate in candidates}
    assert by_lane["W2A"]["is_selected"] is True
    assert by_lane["W2A"]["safety_status"] == "NOT_EVALUATED"
    assert by_lane["W2A"]["annotations"]["route_status"] == "DISABLED"
    assert by_lane["W6A"]["is_selected"] is False
    assert by_lane["W6A"]["safety_status"] == "NOT_EVALUATED"
    assert by_lane["W6A"]["suppressed_by_lane_code"] == "W2A"
    assert {
        candidate["annotations"]["observation_stage"]
        for candidate in candidates
    } == {"post_disabled_research_pre_execution_guards"}
    assert {
        candidate["annotations"]["order_api_calls"] for candidate in candidates
    } == {0}


def test_observation_write_failure_is_fail_open_for_paid_path(monkeypatch) -> None:
    repository = _RecordingRepository(fail=True)
    manager = _manager(repository)
    features = _features()
    selected = select_codex_v1_lane(features)
    monkeypatch.setattr(one_run_module.time, "time", lambda: 240.001)

    async def exercise() -> None:
        await manager._v1469_record_lane_observation(
            {"run_id": "cry3mn-fail-open"},
            features,
            selector_decision=selected,
            effective_decision=selected,
        )
        await manager.shutdown_v1469_observation_writer()

    asyncio.run(exercise())

    assert len(repository.calls) == 1
    assert manager._v1469_observed_opportunity_ids == set()


def test_process_local_observation_dedup_cache_is_bounded(monkeypatch) -> None:
    repository = _RecordingRepository()
    manager = _manager(repository)
    manager._v1469_observed_opportunity_ids = {
        f"old-opportunity-{index}"
        for index in range(manager.V1469_OBSERVATION_SEEN_MAX)
    }
    features = _features()
    selected = select_codex_v1_lane(features)
    monkeypatch.setattr(one_run_module.time, "time", lambda: 360.001)

    async def exercise() -> None:
        await manager._v1469_record_lane_observation(
            {"run_id": "cry3mn-bounded-cache"},
            features,
            selector_decision=selected,
            effective_decision=selected,
        )
        await manager.shutdown_v1469_observation_writer()

    asyncio.run(exercise())

    assert len(repository.calls) == 1
    assert len(manager._v1469_observed_opportunity_ids) == 1


def test_slow_repository_never_blocks_paid_gate(monkeypatch) -> None:
    class SlowRepository(_RecordingRepository):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def insert_observation(
            self,
            opportunity: dict,
            candidates: tuple[dict, ...],
        ) -> None:
            self.calls.append((opportunity, candidates))
            self.started.set()
            await self.release.wait()

    async def exercise() -> None:
        repository = SlowRepository()
        manager = _manager(repository)
        features = _features()
        selected = select_codex_v1_lane(features)
        await asyncio.wait_for(
            manager._v1469_record_lane_observation(
                {"run_id": "cry3mn-nonblocking"},
                features,
                selector_decision=selected,
                effective_decision=selected,
            ),
            timeout=0.05,
        )
        await asyncio.wait_for(repository.started.wait(), timeout=0.05)
        assert len(repository.calls) == 1
        repository.release.set()
        await manager.shutdown_v1469_observation_writer()
        assert not manager._v1469_observation_tasks

    monkeypatch.setattr(one_run_module.time, "time", lambda: 480.001)
    asyncio.run(exercise())


def test_observation_regime_is_symbol_level_and_never_advances_paid_fsm(
    monkeypatch,
) -> None:
    repository = _RecordingRepository()
    manager = _manager(repository)
    features = _features()
    selected = select_codex_v1_lane(features)
    monkeypatch.setattr(one_run_module.time, "time", lambda: 600.001)
    monkeypatch.setattr(
        manager,
        "_v1461_coarse_regime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("paid regime FSM must not be called")
        ),
    )

    async def exercise() -> None:
        await manager._v1469_record_lane_observation(
            {"run_id": "cry3mn-shadow-fsm", "symbol": "ETHUSDC"},
            features,
            selector_decision=selected,
            effective_decision=selected,
            regime_market_state="TREND_UP",
        )
        await manager.shutdown_v1469_observation_writer()

    asyncio.run(exercise())

    assert manager._v1459_regime_runtimes == {}
    assert set(manager._v1469_observation_regime_runtimes) == {
        "ETHUSDC"
    }
    opportunity, _candidates = repository.calls[0]
    snapshot = opportunity["feature_snapshot"]
    assert snapshot["market_state"] == "TREND_UP"
    assert snapshot["v1469_regime_market_state"] == "TREND_UP"


def test_shared_aggtrade_cache_reuses_one_fetch_for_all_lane_arms(monkeypatch):
    class Client:
        def __init__(self):
            self.calls = 0

        async def get_agg_trades(self, *_args, **_kwargs):
            self.calls += 1
            return [{"a": 1, "T": 1_100, "p": "100", "q": "1"}]

    manager = object.__new__(MainnetOneRunManager)
    manager._client = Client()
    manager._settings = SimpleNamespace(
        mainnet_codex_v1460_weak_shadow_max_pages=10,
        mainnet_codex_v1464_shadow_aggtrade_pages_per_cycle=1,
        mainnet_codex_v1460_weak_shadow_page_limit=1000,
    )
    manager._v1461_shadow_aggtrade_caches = {}
    monkeypatch.setattr(one_run_module.time, "time", lambda: 2.0)
    samples = tuple(
        {"symbol": "ETHUSDC", "start_ms": 1_000, "lane": lane}
        for lane in ("W2A", "W6A", "CNL-WPR-L")
    )

    async def exercise():
        first = await manager._v1461_advance_shadow_aggtrade_cache(
            "shared-run", samples, 1_500
        )
        second = await manager._v1461_advance_shadow_aggtrade_cache(
            "shared-run", samples, 1_500
        )
        return first, second

    first, second = asyncio.run(exercise())
    assert first is second
    assert manager._client.calls == 1
    assert first["coverage_end_ms"] == 1_500


def test_shared_aggtrade_cache_expiry_and_entry_bound(monkeypatch):
    manager = object.__new__(MainnetOneRunManager)
    manager._client = SimpleNamespace(get_agg_trades=None)
    manager._settings = SimpleNamespace()
    manager._v1461_shadow_aggtrade_caches = {
        f"old-{index}|ETHUSDC": {
            "last_access_ms": index,
            "coverage_start_ms": 0,
            "coverage_end_ms": 0,
            "rows": [],
        }
        for index in range(manager.V1469_AGGTRADE_CACHE_MAX_ENTRIES)
    }
    monkeypatch.setattr(one_run_module.time, "time", lambda: 10_000.0)

    asyncio.run(manager._v1461_advance_shadow_aggtrade_cache(
        "new-run", ({"symbol": "ETHUSDC", "start_ms": 1},), 2
    ))

    assert len(manager._v1461_shadow_aggtrade_caches) == 1
    assert "new-run|ETHUSDC" in manager._v1461_shadow_aggtrade_caches
