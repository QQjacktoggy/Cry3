from __future__ import annotations

from dataclasses import asdict, replace
import json
import time

import pytest

from src.gridbot.mainnet.v1464_promotion_runtime import PromotionRevalidation
from tests.test_mainnet_one_run_maker import FakeClient, FakeRepo
from tests.test_v1460_one_run_integration import _codex, _manager, _wildcat
from tests.test_v1462_one_run_integration import (
    _ordinary_codex_run,
    _rp1_control,
    _settings as _v1462_settings,
)
from tests.test_v1464_promotion_repository import _repository
from tests.test_v1464_promotion_runtime import (
    CONFIG,
    _cohort,
    _identity,
    _regime,
    _runtime,
)


def _formal_shadow_sample(
    now_ms: int,
    *,
    sample_id: str = "strict-sample-1",
    opportunity_id: str = "legacy-opportunity-1",
    v1462_opportunity_id: str = "v1462-opportunity-1",
) -> dict:
    return {
        **_identity(),
        "classifier_side": "LONG",
        "effective_side": "LONG",
        "raw_accepted": True,
        "pre_gate_accepted": True,
        "final_incumbent_accepted": True,
        "reject_lineage": [],
        "sample_id": sample_id,
        "strict_sample_id": sample_id,
        "opportunity_id": opportunity_id,
        "v1462_opportunity_id": v1462_opportunity_id,
        "run_id": f"run-{sample_id}",
        "first_seen_run_id": f"run-{sample_id}",
        "last_seen_run_id": f"run-{sample_id}",
        "start_ms": now_ms - 2_000,
        "lane": "W6A",
        "effective_lane": "W6A",
        "side": "LONG",
        "fill_model": "limit_touch",
        # Formal v1.4.63 collector rows are not legacy-promotion eligible, but
        # are explicitly eligible for the v1.4.64 evidence evaluator.
        "promotion_eligible": False,
        "evidence_evaluator_eligible": True,
        "diagnostic_only": False,
        "requested_notional_usdc": 25.0,
        "frozen_execution_plan": {
            "schema": "v1463.frozen-effective-ticket.1",
        },
    }


def _formal_shadow_outcome(now_ms: int) -> dict:
    return {
        "shadow_outcome": "tp1_first",
        "terminal_at_ms": now_ms - 1_000,
        "resolved_at_ms": now_ms - 1_000,
        "filled": True,
        "filled_ts": now_ms - 1_500,
        "data_complete": True,
        "evidence_source": "binance_aggTrade",
        "paper_pnl_usdc_after_fee": 0.08,
    }


def _long_wildcat(notional_usdc: float):
    base = _wildcat(notional_usdc)
    signal = replace(
        base.signal,
        action="BUY",
        stop_loss=99.90,
        take_profits=[100.10],
    )
    return replace(
        base,
        signal=signal,
        strategy="S1_BB_RSI",
        side="LONG",
        tp_pct=0.001,
        sl_pct=0.001,
    )


def _adaptive_codex(now_ms: int):
    cohort = _cohort()
    admission = {
        **_identity(cohort),
        "raw_accepted": True,
        "pre_gate_accepted": True,
        "final_incumbent_accepted": True,
        "reject_lineage": [],
    }
    metrics = {
        "market_state": cohort.market_state,
        "v1462_admission": admission,
        "v1464_adaptive_promotion": {
            "adaptive_authorized": True,
            "cohort_key": cohort.key,
            "evaluated_at_ms": now_ms,
            "notional_cap_usdc": 25.0,
            "applied_notional_usdc": 25.0,
            "regime_snapshot": asdict(
                _regime(cohort=cohort, observed_at_ms=now_ms - 100)
            ),
        },
    }
    return replace(
        _codex(),
        lane=cohort.lane_code,
        lane_code=cohort.lane_code,
        strategy=cohort.strategy,
        side=cohort.effective_side,
        regime=cohort.market_state,
        metrics=metrics,
    )


def _paid_admission_metadata(now_ms: int) -> dict:
    cohort = _cohort()
    return {
        "adaptive_authorized": True,
        "incumbent_control_unchanged": True,
        "state": "PROBATION",
        "reason": "lease_active",
        "evaluation_id": "evaluation-paid-backfill",
        "evaluated_at_ms": now_ms - 2_000,
        **_identity(cohort),
        "promotion_policy_hash": CONFIG.policy_hash,
        "cohort_key": cohort.key,
        "lease_id": "lease-paid-backfill",
        "generation": 1,
        "lease_status": "ACTIVE",
        "lease_phase": "PROBATION",
        "evidence_revision": "revision-paid-backfill",
        "evidence_snapshot_hash": "snapshot-paid-backfill",
        "expires_at_ms": now_ms + 60_000,
        "notional_cap_usdc": 25.0,
        "applied_notional_usdc": 25.0,
    }


def test_promoted_lease_disables_recovery_across_restart() -> None:
    manager = _manager(
        settings=_v1462_settings(
            mainnet_codex_recovery_enabled=True,
            mainnet_recovery_enabled=True,
            mainnet_codex_recovery_lane_codes="CNL-WPR-L",
        )
    )
    run = {
        "run_id": "cry3mn_v1464_promoted_recovery_guard",
        "strategy_label": "codex_v1",
        "signal_json": json.dumps(
            {
                "codex_v1": {
                    "enabled": True,
                    "lane_code": "CNL-WPR-L",
                    "risk_tags": ["v1464_adaptive_lease", "v1464_no_dca"],
                    "metrics": {
                        "v1464_adaptive_promotion": {
                            "adaptive_authorized": True,
                            "state": "PROBATION",
                        }
                    },
                }
            }
        ),
    }

    allowed, reason, lane_code = manager._codex_recovery_status(run)

    assert allowed is False
    assert reason == "v1464_adaptive_recovery_disabled"
    assert lane_code == "CNL-WPR-L"


def _completed_adaptive_run(
    now_ms: int,
    *,
    run_id: str,
) -> dict:
    return {
        "run_id": run_id,
        "status": "COMPLETED",
        "completed_at_ms": now_ms - 1_000,
        "exit_reason": "TRAIL",
        "signal_json": {
            "codex_v1": {
                "metrics": {
                    "v1464_adaptive_promotion": _paid_admission_metadata(now_ms),
                },
            },
        },
    }


def _entry_order_api_count(client: FakeClient) -> int:
    return len(client.market_orders) + len(client.all_orders)


class _BlockingPromotionRuntime:
    config = CONFIG
    database_healthy = True

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.calls: list[dict] = []

    async def revalidate_before_submit(self, metadata, now_ms, **kwargs):
        self.calls.append(
            {
                "metadata": metadata,
                "now_ms": now_ms,
                **kwargs,
            }
        )
        return PromotionRevalidation(False, self.reason)


class _ClaimMustNotRun:
    config = CONFIG
    database_healthy = True

    def __init__(self) -> None:
        self.calls = 0

    async def revalidate_before_submit(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("v1.4.60 allowlisted control must not require v1.4.64")


class _EvaluationMustNotRun:
    config = CONFIG
    database_healthy = True

    def __init__(self) -> None:
        self.calls = 0

    async def evaluate_candidate(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("out-of-registry identity must be quarantined")


@pytest.mark.asyncio
async def test_out_of_registry_lane_is_quarantined_before_runtime_evaluation() -> None:
    repo = FakeRepo()
    settings = _v1462_settings(
        mainnet_codex_v1464_auto_promotion_enabled=False,
    )
    manager = _manager(settings=settings, repo=repo)
    manager._dca_enabled = False
    runtime = _EvaluationMustNotRun()
    manager._v1464_promotion_runtime = runtime
    settings.mainnet_codex_v1464_auto_promotion_enabled = True
    run = _ordinary_codex_run("cry3mn_v1464_out_of_registry")
    unknown = _codex(
        market_state="CNL-L1MR-L:reclaim",
        lane_code="CNL-L1MR-L",
    )
    v1460 = await manager._v1460_apply_lane_policy(run, unknown)

    blocked = await manager._v1462_apply_strict_admission(
        run,
        unknown,
        True,
        unknown,
        v1460,
        wildcat_decision=_wildcat(),
    )

    assert blocked.accepted is False
    assert blocked.reason == "v1462.shadow.rule_not_allowlisted"
    assert runtime.calls == 0
    promotion = blocked.metrics["v1464_adaptive_promotion"]
    assert promotion["adaptive_authorized"] is False
    assert promotion["persistence_healthy"] is True
    assert promotion["reason"] == "v1464.out_of_registry"
    assert promotion["registry_quarantined"] is True
    assert promotion["order_api_calls"] == 0


@pytest.mark.asyncio
async def test_formal_one_run_outcome_projects_authoritative_promotion_row(
    tmp_path,
) -> None:
    db, promotion_repo = await _repository(tmp_path)
    try:
        legacy_repo = FakeRepo()
        manager = _manager(
            settings=_v1462_settings(),
            repo=legacy_repo,
        )
        runtime = _runtime(promotion_repo)
        manager._v1464_promotion_runtime = runtime
        now_ms = int(time.time() * 1_000)
        sample = _formal_shadow_sample(now_ms)

        await manager._log_codex_v1_shadow_outcome(
            sample["sample_id"],
            sample,
            _formal_shadow_outcome(now_ms),
        )

        event = next(
            details
            for _run_id, event_type, details in legacy_repo.events
            if event_type == "entry_codex_v1_shadow_outcome"
        )
        assert event["evidence_evaluator_eligible"] is True
        assert event["promotion_eligible"] is False
        assert event["promotion_counts_as"] == "tp_success"

        rows = await promotion_repo.list_sliding_evidence(
            _cohort(),
            window_start_ms=now_ms - 5_000,
            as_of_ms=now_ms,
        )
        assert len(rows) == 1
        assert rows[0]["opportunity_id"] == sample["v1462_opportunity_id"]
        assert rows[0]["source_type"] == "SHADOW"
        assert bool(rows[0]["data_complete"]) is True
        assert bool(rows[0]["diagnostic_only"]) is False
        assert runtime.database_healthy is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_duplicate_formal_sample_outcome_counts_one_v1462_opportunity(
    tmp_path,
) -> None:
    db, promotion_repo = await _repository(tmp_path)
    try:
        manager = _manager(settings=_v1462_settings(), repo=FakeRepo())
        runtime = _runtime(promotion_repo)
        manager._v1464_promotion_runtime = runtime
        now_ms = int(time.time() * 1_000)
        durable_id = "v1462-shared-opportunity"
        first = _formal_shadow_sample(
            now_ms,
            sample_id="strict-sample-a",
            opportunity_id="legacy-opportunity-a",
            v1462_opportunity_id=durable_id,
        )
        duplicate = _formal_shadow_sample(
            now_ms,
            sample_id="strict-sample-b",
            opportunity_id="legacy-opportunity-b",
            v1462_opportunity_id=durable_id,
        )
        outcome = _formal_shadow_outcome(now_ms)

        await manager._log_codex_v1_shadow_outcome(
            first["sample_id"],
            first,
            outcome,
        )
        should_start, reason = manager._codex_v1_should_start_shadow_sample(
            duplicate
        )
        await manager._log_codex_v1_shadow_outcome(
            duplicate["sample_id"],
            duplicate,
            outcome,
        )

        rows = await promotion_repo.list_sliding_evidence(
            _cohort(),
            window_start_ms=now_ms - 5_000,
            as_of_ms=now_ms,
        )
        assert len(rows) == 1
        assert rows[0]["opportunity_id"] == durable_id
        assert runtime.database_healthy is True
        assert should_start is False
        assert reason is not None and "terminal" in reason
    finally:
        await db.close()


def test_unsupported_regime_resets_consecutive_confirmation_stream() -> None:
    manager = _manager(settings=_v1462_settings())
    cohort = _cohort()

    first = manager._v1464_regime_snapshot(
        cohort,
        observed_at_ms=1_000,
        supportive=True,
    )
    unsupported = manager._v1464_regime_snapshot(
        cohort,
        observed_at_ms=2_000,
        supportive=False,
    )
    next_supportive = manager._v1464_regime_snapshot(
        cohort,
        observed_at_ms=3_000,
        supportive=True,
    )

    assert first.confirmations == 1
    assert unsupported.confirmations == 0
    assert unsupported.confirmation_observed_at_ms == ()
    assert next_supportive.confirmations == 1
    assert next_supportive.confirmation_observed_at_ms == (3_000,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "runtime_reason", "expected_reason", "notional_usdc"),
    [
        (
            "expired",
            "lease_expired_or_changed",
            "lease_expired_or_changed",
            25.0,
        ),
        (
            "over_cap",
            "actual_notional_exceeds_admission",
            "actual_notional_exceeds_admission",
            25.01,
        ),
        (
            "identity_mismatch",
            "must_not_be_called",
            "current_candidate_identity_changed",
            25.0,
        ),
        (
            "cas_replay",
            "admission_claim_replayed",
            "admission_claim_replayed",
            25.0,
        ),
    ],
)
async def test_pre_submit_adaptive_failure_never_reaches_order_api(
    case: str,
    runtime_reason: str,
    expected_reason: str,
    notional_usdc: float,
) -> None:
    now_ms = int(time.time() * 1_000)
    client = FakeClient()
    repo = FakeRepo()
    settings = _v1462_settings(
        mainnet_codex_v1464_auto_promotion_enabled=False,
        mainnet_codex_v1464_submit_max_age_seconds=60,
    )
    manager = _manager(
        settings=settings,
        client=client,
        repo=repo,
    )
    runtime = _BlockingPromotionRuntime(runtime_reason)
    manager._v1464_promotion_runtime = runtime
    # Constructor bootstrapping correctly requires a durable repository when
    # enabled.  This test substitutes the post-bootstrap revalidator only.
    settings.mainnet_codex_v1464_auto_promotion_enabled = True
    codex = _adaptive_codex(now_ms)
    if case == "identity_mismatch":
        codex = replace(codex, lane="W6B", lane_code="W6B")
    run = {
        "run_id": f"cry3mn_v1464_{case}",
        "symbol": "ETHUSDC",
        "status": "ARMED",
        "side": "LONG",
        "strategy": "S1_BB_RSI",
        "armed_at_ms": now_ms,
        "cumulative_notional_usdc": 0.0,
    }

    await manager._place_entry(
        run,
        _long_wildcat(notional_usdc),
        codex_decision=codex,
    )

    assert _entry_order_api_count(client) == 0
    assert client.open_orders == []
    assert client.reduce_only_limit_orders == []
    assert client.stop_market_sl_orders == []
    blocked = [
        details
        for _run_id, event_type, details in repo.events
        if event_type == "entry_codex_v1464_lease_blocked"
    ]
    assert len(blocked) == 1
    assert blocked[0]["reason"] == expected_reason
    assert blocked[0]["order_api_calls"] == 0
    assert len(runtime.calls) == (0 if case == "identity_mismatch" else 1)


@pytest.mark.asyncio
async def test_v1460_allowlisted_control_places_entry_without_v1464_claim() -> None:
    client = FakeClient()
    repo = FakeRepo()
    settings = _v1462_settings(
        mainnet_codex_v1464_auto_promotion_enabled=False,
    )
    manager = _manager(
        settings=settings,
        client=client,
        repo=repo,
    )
    runtime = _ClaimMustNotRun()
    manager._v1464_promotion_runtime = runtime
    settings.mainnet_codex_v1464_auto_promotion_enabled = True
    run = _ordinary_codex_run("cry3mn_v1460_control_without_claim")
    run.update(
        {
            "side": "LONG",
            "strategy": "S1_BB_RSI",
            "cumulative_notional_usdc": 0.0,
        }
    )

    await manager._place_entry(
        run,
        _long_wildcat(50.0),
        codex_decision=_rp1_control(),
    )

    assert runtime.calls == 0
    assert _entry_order_api_count(client) == 1
    assert len(client.open_orders) == 1
    assert any(
        event_type == "entry_placed"
        for _run_id, event_type, _details in repo.events
    )


@pytest.mark.asyncio
async def test_paid_terminal_reconcile_recovers_after_torn_first_projection_once(
    tmp_path,
    monkeypatch,
) -> None:
    db, promotion_repo = await _repository(tmp_path)
    try:
        now_ms = int(time.time() * 1_000)
        run_id = "cry3mn_v1464_paid_reconcile_retry"
        legacy_repo = FakeRepo()
        legacy_repo.recent_runs = [
            _completed_adaptive_run(now_ms, run_id=run_id),
        ]
        await legacy_repo.log_event(
            run_id,
            "completed",
            {
                "eligible_for_wr_ev": True,
                "net_pnl": 0.12,
                "exit_reason_final": "TRAIL",
            },
        )
        manager = _manager(
            settings=_v1462_settings(),
            repo=legacy_repo,
        )
        runtime = _runtime(promotion_repo)
        manager._v1464_promotion_runtime = runtime
        original_enforce = runtime._enforce_paid_risk_after_terminal
        enforcement_calls = 0

        async def fail_once_after_insert(metadata, *, now_ms, source_id):
            nonlocal enforcement_calls
            enforcement_calls += 1
            if enforcement_calls == 1:
                raise RuntimeError("simulated post-insert database failure")
            await original_enforce(
                metadata,
                now_ms=now_ms,
                source_id=source_id,
            )

        monkeypatch.setattr(
            runtime,
            "_enforce_paid_risk_after_terminal",
            fail_once_after_insert,
        )

        with pytest.raises(
            RuntimeError,
            match="simulated post-insert database failure",
        ):
            await manager._v1464_reconcile_paid_terminals()

        assert runtime.database_healthy is False
        assert manager._v1464_paid_terminal_reconciled is False

        await manager._v1464_reconcile_paid_terminals()
        await manager._v1464_reconcile_paid_terminals()

        rows = await promotion_repo.list_sliding_evidence(
            _cohort(),
            window_start_ms=now_ms - 10_000,
            as_of_ms=now_ms + 10_000,
            eligible_only=False,
        )
        paid = [row for row in rows if row["source_type"] == "PAID"]
        assert len(paid) == 1
        assert paid[0]["opportunity_id"] == f"paid:{run_id}"
        assert paid[0]["source_id"] == run_id
        assert paid[0]["net_pnl_usdc"] == pytest.approx(0.12)
        assert enforcement_calls == 2
        assert runtime.database_healthy is True
        assert manager._v1464_paid_terminal_reconciled is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_paid_terminal_reconcile_skips_completed_run_not_wr_ev_eligible(
    tmp_path,
) -> None:
    db, promotion_repo = await _repository(tmp_path)
    try:
        now_ms = int(time.time() * 1_000)
        run_id = "cry3mn_v1464_paid_reconcile_ineligible"
        legacy_repo = FakeRepo()
        legacy_repo.recent_runs = [
            _completed_adaptive_run(now_ms, run_id=run_id),
        ]
        await legacy_repo.log_event(
            run_id,
            "completed",
            {
                "eligible_for_wr_ev": False,
                "net_pnl": 0.12,
                "exit_reason_final": "TRAIL",
            },
        )
        manager = _manager(
            settings=_v1462_settings(),
            repo=legacy_repo,
        )
        runtime = _runtime(promotion_repo)
        manager._v1464_promotion_runtime = runtime

        await manager._v1464_reconcile_paid_terminals()

        rows = await promotion_repo.list_sliding_evidence(
            _cohort(),
            window_start_ms=now_ms - 10_000,
            as_of_ms=now_ms + 10_000,
            eligible_only=False,
        )
        assert [row for row in rows if row["source_type"] == "PAID"] == []
        assert runtime.database_healthy is True
        assert manager._v1464_paid_terminal_reconciled is True
    finally:
        await db.close()
