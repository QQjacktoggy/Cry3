import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
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

class _PairedSuccess:
    async def start_observation(self, opportunity, candidates):
        return {
            "capacity_dropped": 0,
            "evidence_started": (
                1 if opportunity.get("legacy_execution_snapshot") is not None else 0
            ),
        }


class _DelayedRepository(_RecordingRepository):
    def __init__(self) -> None:
        super().__init__()
        self.release: asyncio.Event | None = None

    async def insert_observation(
        self,
        opportunity: dict,
        candidates: tuple[dict, ...],
    ) -> None:
        self.calls.append((opportunity, candidates))
        assert self.release is not None
        await self.release.wait()


def _manager(repository: _RecordingRepository) -> MainnetOneRunManager:
    manager = object.__new__(MainnetOneRunManager)
    manager._settings = SimpleNamespace(
        mainnet_codex_v1469_observation_enabled=True,
        mainnet_codex_v1469_observation_bucket_seconds=30,
    )
    manager._v1469_arm_observation_repo = repository
    manager._v1469_observed_opportunity_ids = set()
    manager._v1469_observation_inflight_ids = set()
    manager._v1469_paid_path_inflight_tokens = set()
    manager._v1469_exact_persistence_owners = {}
    manager._v1469_paid_token_by_task = {}
    manager._v1469_pending_paid_observations = {}
    manager._v1469_bucket_finalizer_tasks = {}
    manager._v1469_observation_tasks = set()
    manager._v1469_observation_backlog = []
    manager._v1469_observation_shutdown = False
    manager._v1469_observation_dropped = 0
    manager._v1469_observation_repo_missing_logged = False
    manager._v1459_regime_runtimes = {}
    manager._v1469_observation_regime_runtimes = {}
    manager._v1469_paired_shadow_runtime = _PairedSuccess()
    manager._v1469_observation_active = lambda run: True
    return manager

def test_exact_source_replay_rejects_incomplete_first_snapshot() -> None:
    class SourceReplayRepository(_RecordingRepository):
        async def insert_observation(self, opportunity, candidates):
            self.calls.append((opportunity, candidates))
            return {
                "source_replay": True,
                "durable_opportunity_id": "durable-degraded",
            }

        async def load_observation_bundle(self, opportunity_id):
            assert opportunity_id == "durable-degraded"
            return {
                "opportunity": {
                    "opportunity_id": "durable-degraded",
                    "data_quality": "DATA_INCOMPLETE",
                },
                "candidates": (),
            }

    class PairedSpy:
        def __init__(self) -> None:
            self.calls = []

        async def start_observation(self, opportunity, candidates):
            self.calls.append((opportunity, candidates))
            return {"capacity_dropped": 0}

    repository = SourceReplayRepository()
    manager = _manager(repository)
    manager._v1469_paired_shadow_runtime = PairedSpy()

    async def exercise() -> None:
        with pytest.raises(
            RuntimeError,
            match="incomplete first snapshot",
        ):
            await manager._v1469_persist_lane_observation(
                repository=repository,
                run_id="source-replay-degraded",
                dedup_key="same-bucket",
                opportunity={
                    "opportunity_id": "incoming-exact",
                    "legacy_execution_snapshot": object(),
                },
                candidates=(),
                raise_on_error=True,
            )

    asyncio.run(exercise())
    assert manager._v1469_paired_shadow_runtime.calls == []



def test_rejected_sibling_timer_waits_for_inflight_exact_evaluator(
    monkeypatch,
) -> None:
    repository = _RecordingRepository()
    manager = _manager(repository)
    manager._settings.mainnet_codex_v1469_observation_bucket_seconds = 1
    selected = select_codex_v1_lane(_features())
    monkeypatch.setattr(one_run_module.time, "time", lambda: 0.99)
    exact_legacy_snapshot = object()
    manager._v1469_build_resolved_legacy_snapshot = (
        lambda *args, **kwargs: exact_legacy_snapshot
    )

    async def exercise() -> None:
        run = {"run_id": "sibling-timer-exact-winner"}
        await manager._v1469_record_lane_observation(
            run,
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        manager._v1469_flush_adaptive_only(run)
        await manager._v1469_record_lane_observation(
            run,
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        await asyncio.sleep(0.05)
        assert repository.calls == []
        durable_id = await manager._v1469_finish_paid_observation(
            run,
            SimpleNamespace(),
            selected,
            entry_signal_price=2000.0,
            entry_offset_bp=0.0,
            entry_notional=50.0,
        )
        assert durable_id
        await manager.shutdown_v1469_observation_writer()

    asyncio.run(exercise())
    assert len(repository.calls) == 1
    opportunity, _candidates = repository.calls[0]
    assert opportunity["data_quality"] == "COMPLETE"
    assert opportunity["legacy_execution_snapshot"] is exact_legacy_snapshot

def test_rejected_sibling_waits_while_exact_writer_is_persisting(
    monkeypatch,
) -> None:
    class BlockingRepository(_RecordingRepository):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def insert_observation(self, opportunity, candidates):
            self.calls.append((opportunity, candidates))
            self.started.set()
            await self.release.wait()

    async def exercise() -> None:
        repository = BlockingRepository()
        manager = _manager(repository)
        manager._settings.mainnet_codex_v1469_observation_bucket_seconds = 1
        selected = select_codex_v1_lane(_features())
        monkeypatch.setattr(one_run_module.time, "time", lambda: 0.99)
        manager._v1469_build_resolved_legacy_snapshot = (
            lambda *args, **kwargs: object()
        )
        run = {"run_id": "exact-writer-owns-bucket"}
        await manager._v1469_record_lane_observation(
            run,
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        manager._v1469_flush_adaptive_only(run)
        async def exact_evaluator() -> str | None:
            await manager._v1469_record_lane_observation(
                run,
                _features(),
                selector_decision=selected,
                effective_decision=selected,
                reference_price=2000.0,
            )
            return await manager._v1469_finish_paid_observation(
                run,
                SimpleNamespace(),
                selected,
                entry_signal_price=2000.0,
                entry_offset_bp=0.0,
                entry_notional=50.0,
            )

        finish = asyncio.create_task(exact_evaluator())
        await asyncio.wait_for(repository.started.wait(), timeout=0.2)
        await asyncio.sleep(0.05)
        assert len(repository.calls) == 1
        assert repository.calls[0][0]["data_quality"] == "COMPLETE"
        assert sum(manager._v1469_exact_persistence_owners.values()) == 1
        repository.release.set()
        assert await finish
        await asyncio.sleep(0)
        assert manager._v1469_pending_paid_observations == {}
        assert manager._v1469_exact_persistence_owners == {}
        await manager.shutdown_v1469_observation_writer()

    asyncio.run(exercise())


def test_concurrent_exact_writers_release_refcount_independently(
    monkeypatch,
) -> None:
    class SplitRepository(_RecordingRepository):
        def __init__(self) -> None:
            super().__init__()
            self.both_started = asyncio.Event()
            self.fail_first = asyncio.Event()
            self.release_second = asyncio.Event()

        async def insert_observation(self, opportunity, candidates):
            call_index = len(self.calls)
            self.calls.append((opportunity, candidates))
            if len(self.calls) == 2:
                self.both_started.set()
            if call_index == 0:
                await self.fail_first.wait()
                raise RuntimeError("first exact writer failed")
            await self.release_second.wait()

    async def exercise() -> None:
        repository = SplitRepository()
        manager = _manager(repository)
        manager._settings.mainnet_codex_v1469_observation_bucket_seconds = 1
        selected = select_codex_v1_lane(_features())
        monkeypatch.setattr(one_run_module.time, "time", lambda: 0.99)
        manager._v1469_build_resolved_legacy_snapshot = (
            lambda *args, **kwargs: kwargs["entry_signal_price"]
        )
        run = {"run_id": "two-exact-writers"}
        await manager._v1469_record_lane_observation(
            run,
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        manager._v1469_flush_adaptive_only(run)
        launch = asyncio.Event()
        ready_a, ready_b = asyncio.Event(), asyncio.Event()

        async def evaluator(ready: asyncio.Event, price: float) -> str | None:
            await manager._v1469_record_lane_observation(
                run,
                _features(),
                selector_decision=selected,
                effective_decision=selected,
                reference_price=2000.0,
            )
            ready.set()
            await launch.wait()
            return await manager._v1469_finish_paid_observation(
                run,
                SimpleNamespace(),
                selected,
                entry_signal_price=price,
                entry_offset_bp=0.0,
                entry_notional=50.0,
            )

        task_a = asyncio.create_task(evaluator(ready_a, 2001.0))
        task_b = asyncio.create_task(evaluator(ready_b, 2002.0))
        await asyncio.gather(ready_a.wait(), ready_b.wait())
        launch.set()
        await asyncio.wait_for(repository.both_started.wait(), timeout=0.2)
        assert sum(manager._v1469_exact_persistence_owners.values()) == 2

        repository.fail_first.set()
        done, pending = await asyncio.wait(
            {task_a, task_b},
            timeout=0.2,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert len(done) == 1
        assert next(iter(done)).result() is None
        assert len(pending) == 1
        assert sum(manager._v1469_exact_persistence_owners.values()) == 1
        assert manager._v1469_finalize_expired_pending(1_000) == 0
        await asyncio.sleep(0.05)
        assert len(repository.calls) == 2

        repository.release_second.set()
        remaining_result = await next(iter(pending))
        assert remaining_result
        await asyncio.sleep(0)
        assert manager._v1469_exact_persistence_owners == {}
        assert manager._v1469_pending_paid_observations == {}
        await manager.shutdown_v1469_observation_writer()

    asyncio.run(exercise())

def test_capacity_eviction_never_evicts_bucket_with_inflight_sibling(
    monkeypatch,
) -> None:
    repository = _RecordingRepository()
    manager = _manager(repository)
    manager.V1469_PENDING_PAID_MAX = 2
    manager._v1469_pending_paid_observations = {
        "released": ("same-run", "same-dedup", {}, ()),
        "inflight": ("same-run", "same-dedup", {}, ()),
    }
    manager._v1469_paid_path_inflight_tokens = {"inflight"}
    selected = select_codex_v1_lane(_features())
    monkeypatch.setattr(one_run_module.time, "time", lambda: 30.001)

    async def exercise() -> None:
        token = await manager._v1469_record_lane_observation(
            {"run_id": "new-run"},
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        assert token is None

    asyncio.run(exercise())
    assert set(manager._v1469_pending_paid_observations) == {
        "released",
        "inflight",
    }
    assert repository.calls == []
    assert manager._v1469_observation_dropped == 1


def test_exact_barrier_fails_when_paired_runtime_is_absent(
    monkeypatch,
) -> None:
    repository = _RecordingRepository()
    manager = _manager(repository)
    manager._v1469_paired_shadow_runtime = None
    selected = select_codex_v1_lane(_features())
    monkeypatch.setattr(one_run_module.time, "time", lambda: 60.001)
    manager._v1469_build_resolved_legacy_snapshot = (
        lambda *args, **kwargs: object()
    )

    async def exercise() -> None:
        run = {"run_id": "paired-runtime-required"}
        await manager._v1469_record_lane_observation(
            run,
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        assert await manager._v1469_finish_paid_observation(
            run,
            SimpleNamespace(),
            selected,
            entry_signal_price=2000.0,
            entry_offset_bp=0.0,
            entry_notional=50.0,
        ) is None
        await asyncio.sleep(0)
        assert manager._v1469_exact_persistence_owners == {}
        await manager.shutdown_v1469_observation_writer()

    asyncio.run(exercise())
    assert len(repository.calls) == 1

def test_shutdown_drain_respects_writer_concurrency_limit() -> None:
    class ConcurrencyRepository(_RecordingRepository):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def insert_observation(self, opportunity, candidates):
            self.calls.append((opportunity, candidates))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active >= 2:
                self.started.set()
            try:
                await self.release.wait()
            finally:
                self.active -= 1

    async def exercise() -> None:
        repository = ConcurrencyRepository()
        manager = _manager(repository)
        manager.V1469_OBSERVATION_MAX_INFLIGHT = 2
        manager.V1469_OBSERVATION_BACKLOG_MAX = 8
        accepted = [
            manager._v1469_schedule_observation(
                repository,
                f"shutdown-run-{index}",
                f"shutdown-dedup-{index}",
                {"opportunity_id": f"shutdown-opp-{index}"},
                (),
            )
            for index in range(8)
        ]
        assert all(accepted)
        shutdown = asyncio.create_task(
            manager.shutdown_v1469_observation_writer(timeout_seconds=2.0)
        )
        await asyncio.wait_for(repository.started.wait(), timeout=0.2)
        assert repository.max_active == 2
        assert len(manager._v1469_observation_tasks) <= 2
        repository.release.set()
        await shutdown
        assert repository.max_active == 2
        assert len(repository.calls) == 8
        assert not manager._v1469_observation_tasks
        assert manager._v1469_observation_backlog == []

    asyncio.run(exercise())

def test_deferred_accepted_observation_waits_for_bucket_close(
    monkeypatch,
) -> None:
    repository = _RecordingRepository()
    manager = _manager(repository)
    selected = select_codex_v1_lane(_features())
    clock = [90.001]
    monkeypatch.setattr(one_run_module.time, "time", lambda: clock[0])

    async def exercise() -> None:
        run = {"run_id": "deferred-slow-path"}
        await manager._v1469_record_lane_observation(
            run,
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        assert len(manager._v1469_pending_paid_observations) == 1
        await asyncio.sleep(0.03)
        assert repository.calls == []

        # Releasing the current evaluator inside the fixed bucket must leave
        # the snapshot replaceable for a later paid-path retry.
        manager._v1469_flush_adaptive_only(run)
        assert repository.calls == []
        assert len(manager._v1469_pending_paid_observations) == 1

        clock[0] = 120.0
        assert manager._v1469_finalize_expired_pending(120_000) == 1
        await manager.shutdown_v1469_observation_writer()
        assert manager._v1469_pending_paid_observations == {}
        assert manager._v1469_observation_backlog == []

    asyncio.run(exercise())

    assert len(repository.calls) == 1
    opportunity, candidates = repository.calls[0]
    assert opportunity["data_quality"] == "DATA_INCOMPLETE"
    assert candidates[0]["safety_status"] == "DATA_BLOCKED"
    assert candidates[0]["annotations"]["exact_snapshot_data_blocked"] == (
        "exact_snapshot_data_blocked:bucket_closed_without_paid_finalization"
    )
def test_shutdown_flushes_deferred_context_and_drains_backlog(
    monkeypatch,
) -> None:
    repository = _RecordingRepository()
    manager = _manager(repository)
    selected = select_codex_v1_lane(_features())
    monkeypatch.setattr(one_run_module.time, "time", lambda: 105.001)

    async def exercise() -> None:
        await manager._v1469_record_lane_observation(
            {"run_id": "deferred-shutdown"},
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        await manager.shutdown_v1469_observation_writer()
        assert manager._v1469_pending_paid_observations == {}
        assert manager._v1469_observation_backlog == []
        assert not manager._v1469_observation_tasks

    asyncio.run(exercise())

    assert len(repository.calls) == 1
    _, candidates = repository.calls[0]
    assert candidates[0]["annotations"]["exact_snapshot_data_blocked"] == (
        "exact_snapshot_data_blocked:shutdown_flush"
    )


def test_deferred_bucket_close_is_exactly_once(monkeypatch) -> None:
    repository = _RecordingRepository()
    manager = _manager(repository)
    selected = select_codex_v1_lane(_features())
    clock = [110.001]
    monkeypatch.setattr(one_run_module.time, "time", lambda: clock[0])

    async def exercise() -> None:
        run = {"run_id": "deferred-boundary"}
        await manager._v1469_record_lane_observation(
            run,
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        manager._v1469_flush_adaptive_only(run)
        manager._v1469_flush_adaptive_only(run)
        assert repository.calls == []
        clock[0] = 120.0
        assert manager._v1469_finalize_expired_pending(120_000) == 1
        assert manager._v1469_finalize_expired_pending(120_000) == 0
        await manager.shutdown_v1469_observation_writer()

    asyncio.run(exercise())
    assert len(repository.calls) == 1
def test_deferred_capacity_eviction_persists_every_context(monkeypatch) -> None:
    repository = _RecordingRepository()
    manager = _manager(repository)
    manager.V1469_PENDING_PAID_MAX = 1
    selected = select_codex_v1_lane(_features())
    clock = [120.001]
    monkeypatch.setattr(one_run_module.time, "time", lambda: clock[0])

    async def exercise() -> None:
        old_run = {"run_id": "capacity-old"}
        await manager._v1469_record_lane_observation(
            old_run,
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        manager._v1469_flush_adaptive_only(old_run)
        clock[0] = 121.001
        await manager._v1469_record_lane_observation(
            {"run_id": "capacity-new"},
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        await manager.shutdown_v1469_observation_writer()

    asyncio.run(exercise())
    assert len(repository.calls) == 2
    reasons = {
        call[1][0]["annotations"]["exact_snapshot_data_blocked"]
        for call in repository.calls
    }
    assert reasons == {
        "exact_snapshot_data_blocked:deferred_capacity_evicted",
        "exact_snapshot_data_blocked:shutdown_flush",
    }

def test_same_bucket_retry_can_attach_exact_legacy_snapshot(monkeypatch) -> None:
    repository = _RecordingRepository()
    manager = _manager(repository)
    selected = select_codex_v1_lane(_features())
    clock = [90.001]
    monkeypatch.setattr(one_run_module.time, "time", lambda: clock[0])
    exact_legacy_snapshot = object()
    manager._v1469_build_resolved_legacy_snapshot = (
        lambda *args, **kwargs: exact_legacy_snapshot
    )

    async def exercise() -> None:
        run = {"run_id": "same-bucket-retry"}
        await manager._v1469_record_lane_observation(
            run,
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        manager._v1469_flush_adaptive_only(run)
        assert repository.calls == []

        clock[0] = 95.001
        await manager._v1469_record_lane_observation(
            run,
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        durable_id = await manager._v1469_finish_paid_observation(
            run,
            SimpleNamespace(),
            selected,
            entry_signal_price=2000.0,
            entry_offset_bp=0.0,
            entry_notional=50.0,
        )
        assert durable_id
        manager._v1469_flush_adaptive_only(run)
        await manager.shutdown_v1469_observation_writer()

    asyncio.run(exercise())

    assert len(repository.calls) == 1
    opportunity, _candidates = repository.calls[0]
    assert opportunity["data_quality"] == "COMPLETE"
    assert opportunity["legacy_execution_snapshot"] is exact_legacy_snapshot

def test_bucket_timer_finalizes_without_another_evaluator(monkeypatch) -> None:
    repository = _RecordingRepository()
    manager = _manager(repository)
    manager._settings.mainnet_codex_v1469_observation_bucket_seconds = 1
    selected = select_codex_v1_lane(_features())
    monkeypatch.setattr(one_run_module.time, "time", lambda: 0.99)

    async def exercise() -> None:
        run = {"run_id": "timer-boundary"}
        await manager._v1469_record_lane_observation(
            run,
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        manager._v1469_flush_adaptive_only(run)
        await asyncio.sleep(0.05)
        assert len(repository.calls) == 1
        assert manager._v1469_pending_paid_observations == {}
        await manager.shutdown_v1469_observation_writer()

    asyncio.run(exercise())


def test_failed_boundary_enqueue_retains_staged_copy_for_shutdown(
    monkeypatch,
) -> None:
    repository = _RecordingRepository()
    manager = _manager(repository)
    manager.V1469_OBSERVATION_MAX_INFLIGHT = 0
    manager.V1469_OBSERVATION_BACKLOG_MAX = 0
    selected = select_codex_v1_lane(_features())
    clock = [90.001]
    monkeypatch.setattr(one_run_module.time, "time", lambda: clock[0])

    async def exercise() -> None:
        run = {"run_id": "retain-on-full"}
        await manager._v1469_record_lane_observation(
            run,
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        manager._v1469_flush_adaptive_only(run)
        clock[0] = 120.0
        assert manager._v1469_finalize_expired_pending(120_000) == 0
        assert len(manager._v1469_pending_paid_observations) == 1
        await manager.shutdown_v1469_observation_writer()
        assert manager._v1469_pending_paid_observations == {}

    asyncio.run(exercise())
    assert len(repository.calls) == 1


def test_overlapping_same_run_evaluators_never_cross_attach_snapshot(
    monkeypatch,
) -> None:
    repository = _RecordingRepository()
    manager = _manager(repository)
    selected = select_codex_v1_lane(_features())
    monkeypatch.setattr(one_run_module.time, "time", lambda: 90.001)
    manager._v1469_build_resolved_legacy_snapshot = (
        lambda *args, **kwargs: kwargs["entry_signal_price"]
    )

    async def exercise() -> None:
        run = {"run_id": "overlap-token-owner"}
        ready_a, ready_b = asyncio.Event(), asyncio.Event()
        release_a, release_b = asyncio.Event(), asyncio.Event()

        async def evaluator(
            ready: asyncio.Event,
            release: asyncio.Event,
            entry_price: float,
        ) -> str | None:
            await manager._v1469_record_lane_observation(
                run,
                _features(),
                selector_decision=selected,
                effective_decision=selected,
                reference_price=2000.0,
            )
            ready.set()
            await release.wait()
            return await manager._v1469_finish_paid_observation(
                run,
                SimpleNamespace(),
                selected,
                entry_signal_price=entry_price,
                entry_offset_bp=0.0,
                entry_notional=50.0,
            )

        task_a = asyncio.create_task(evaluator(ready_a, release_a, 2001.0))
        task_b = asyncio.create_task(evaluator(ready_b, release_b, 2002.0))
        await asyncio.gather(ready_a.wait(), ready_b.wait())
        assert len(manager._v1469_pending_paid_observations) == 2
        release_b.set()
        result_b = await task_b
        release_a.set()
        result_a = await task_a
        assert result_b
        assert result_a is None
        await manager.shutdown_v1469_observation_writer()

    asyncio.run(exercise())
    assert len(repository.calls) == 1
    assert repository.calls[0][0]["legacy_execution_snapshot"] == 2002.0


def test_exact_barrier_returns_none_when_paired_capacity_drops(
    monkeypatch,
) -> None:
    class CapacityDropRuntime:
        async def start_observation(self, opportunity, candidates):
            return {"capacity_dropped": 3, "evidence_started": 3}

    repository = _RecordingRepository()
    manager = _manager(repository)
    manager._v1469_paired_shadow_runtime = CapacityDropRuntime()
    selected = select_codex_v1_lane(_features())
    monkeypatch.setattr(one_run_module.time, "time", lambda: 180.001)
    manager._v1469_build_resolved_legacy_snapshot = (
        lambda *args, **kwargs: object()
    )

    async def exercise() -> None:
        run = {"run_id": "exact-capacity-drop"}
        await manager._v1469_record_lane_observation(
            run,
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        manager._v1469_flush_adaptive_only(run)
        await manager._v1469_record_lane_observation(
            run,
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        durable_id = await manager._v1469_finish_paid_observation(
            run,
            SimpleNamespace(),
            selected,
            entry_signal_price=2000.0,
            entry_offset_bp=0.0,
            entry_notional=50.0,
        )
        assert durable_id is None
        await asyncio.sleep(0)
        assert manager._v1469_pending_paid_observations == {}
        assert manager._v1469_exact_persistence_owners == {}
        await manager.shutdown_v1469_observation_writer()

    asyncio.run(exercise())
    assert len(repository.calls) == 1


def test_source_replay_uses_loaded_durable_bundle() -> None:
    class SourceReplayRepository(_RecordingRepository):
        async def insert_observation(self, opportunity, candidates):
            self.calls.append((opportunity, candidates))
            return {
                "source_replay": True,
                "durable_opportunity_id": "durable-first",
            }

        async def load_observation_bundle(self, opportunity_id):
            assert opportunity_id == "durable-first"
            return {
                "opportunity": {
                    "opportunity_id": "durable-first",
                    "data_quality": "COMPLETE",
                },
                "candidates": (
                    {
                        "opportunity_id": "durable-first",
                        "candidate_id": "durable-candidate",
                    },
                ),
            }

    class PairedSpy:
        def __init__(self) -> None:
            self.calls = []

        async def start_observation(self, opportunity, candidates):
            self.calls.append((opportunity, candidates))
            return {"capacity_dropped": 0}

    repository = SourceReplayRepository()
    manager = _manager(repository)
    manager._v1469_paired_shadow_runtime = PairedSpy()

    async def exercise() -> None:
        outcome = await manager._v1469_persist_lane_observation(
            repository=repository,
            run_id="source-replay",
            dedup_key="same-bucket",
            opportunity={"opportunity_id": "incoming-second"},
            candidates=(),
        )
        assert outcome["durable_opportunity_id"] == "durable-first"
        assert outcome["source_replay"] is True

    asyncio.run(exercise())
    paired_opportunity, paired_candidates = (
        manager._v1469_paired_shadow_runtime.calls[0]
    )
    assert paired_opportunity["opportunity_id"] == "durable-first"
    assert paired_candidates[0]["candidate_id"] == "durable-candidate"


def test_shutdown_latch_rejects_new_observation(monkeypatch) -> None:
    repository = _RecordingRepository()
    manager = _manager(repository)
    selected = select_codex_v1_lane(_features())
    monkeypatch.setattr(one_run_module.time, "time", lambda: 210.001)

    async def exercise() -> None:
        await manager.shutdown_v1469_observation_writer()
        token = await manager._v1469_record_lane_observation(
            {"run_id": "after-shutdown"},
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        assert token is None
        assert manager._v1469_pending_paid_observations == {}

    asyncio.run(exercise())
    assert repository.calls == []

def test_snapshot_build_failure_persists_schema_valid_blocked_bundle(
    monkeypatch,
) -> None:
    repository = _RecordingRepository()
    manager = _manager(repository)
    selected = select_codex_v1_lane(_features())
    monkeypatch.setattr(one_run_module.time, "time", lambda: 180.001)

    async def exercise() -> None:
        run = {"run_id": "snapshot-build-failure"}
        await manager._v1469_record_lane_observation(
            run,
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )

        def fail_snapshot(*args, **kwargs):
            raise RuntimeError("unsupported_geometry")

        manager._v1469_build_resolved_legacy_snapshot = fail_snapshot
        durable_id = await manager._v1469_finish_paid_observation(
            run,
            SimpleNamespace(),
            selected,
            entry_signal_price=2000.0,
            entry_offset_bp=0.0,
            entry_notional=50.0,
        )
        assert durable_id is None
        await manager.shutdown_v1469_observation_writer()

    asyncio.run(exercise())

    assert len(repository.calls) == 1
    opportunity, candidates = repository.calls[0]
    assert opportunity["data_quality"] == "DATA_INCOMPLETE"
    assert "legacy_execution_snapshot" not in opportunity
    assert candidates[0]["safety_status"] == "DATA_BLOCKED"
    assert candidates[0]["annotations"]["exact_snapshot_data_blocked"] == (
        "exact_snapshot_data_blocked:RuntimeError:unsupported_geometry"
    )


def test_durability_timeout_writer_remains_tracked_until_shutdown(
    monkeypatch,
) -> None:
    repository = _DelayedRepository()
    manager = _manager(repository)
    manager.V1469_DURABILITY_BARRIER_SECONDS = 0.01
    selected = select_codex_v1_lane(_features())
    monkeypatch.setattr(one_run_module.time, "time", lambda: 210.001)
    manager._v1469_build_resolved_legacy_snapshot = (
        lambda *args, **kwargs: object()
    )

    async def exercise() -> None:
        repository.release = asyncio.Event()
        run = {"run_id": "durability-timeout"}
        await manager._v1469_record_lane_observation(
            run,
            _features(),
            selector_decision=selected,
            effective_decision=selected,
            reference_price=2000.0,
        )
        durable_id = await manager._v1469_finish_paid_observation(
            run,
            SimpleNamespace(),
            selected,
            entry_signal_price=2000.0,
            entry_offset_bp=0.0,
            entry_notional=50.0,
        )
        assert durable_id is None
        assert len(manager._v1469_observation_tasks) == 1
        repository.release.set()
        await manager.shutdown_v1469_observation_writer(timeout_seconds=1.0)
        assert not manager._v1469_observation_tasks
        assert manager._v1469_observation_backlog == []

    asyncio.run(exercise())
    assert len(repository.calls) == 1


def test_observation_backlog_is_bounded_and_drained() -> None:
    repository = _DelayedRepository()
    manager = _manager(repository)
    manager.V1469_OBSERVATION_MAX_INFLIGHT = 1
    manager.V1469_OBSERVATION_BACKLOG_MAX = 1

    async def exercise() -> None:
        repository.release = asyncio.Event()
        accepted = []
        for index in range(3):
            accepted.append(
                manager._v1469_schedule_observation(
                    repository,
                    f"run-{index}",
                    f"dedup-{index}",
                    {"opportunity_id": f"opp-{index}"},
                    (),
                )
            )
        assert accepted == [True, True, False]
        assert len(manager._v1469_observation_tasks) == 1
        assert len(manager._v1469_observation_backlog) == 1
        assert manager._v1469_observation_dropped == 1
        repository.release.set()
        await manager.shutdown_v1469_observation_writer(timeout_seconds=1.0)
        assert not manager._v1469_observation_tasks
        assert manager._v1469_observation_backlog == []

    asyncio.run(exercise())
    assert len(repository.calls) == 2

def test_paired_capacity_drop_keeps_dedup_repairable() -> None:
    class PairedRuntime:
        def __init__(self) -> None:
            self.calls = 0

        async def start_observation(self, opportunity, candidates):
            self.calls += 1
            return {"capacity_dropped": 1 if self.calls == 1 else 0}

    repository = _RecordingRepository()
    manager = _manager(repository)
    manager._v1469_paired_shadow_runtime = PairedRuntime()

    async def exercise() -> None:
        kwargs = {
            "repository": repository,
            "run_id": "paired-repair",
            "dedup_key": "paired-dedup",
            "opportunity": {"opportunity_id": "paired-opportunity"},
            "candidates": (),
        }
        first = await manager._v1469_persist_lane_observation(**kwargs)
        assert first["paired_complete"] is False
        assert "paired-dedup" not in manager._v1469_observed_opportunity_ids
        second = await manager._v1469_persist_lane_observation(**kwargs)
        assert second["paired_complete"] is True
        assert "paired-dedup" in manager._v1469_observed_opportunity_ids

    asyncio.run(exercise())
    assert manager._v1469_paired_shadow_runtime.calls == 2

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
