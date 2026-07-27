import json

import pytest

from src.gridbot.mainnet.v1459_cohort_tracking import (
    TRACKING_CONFIG_KEY,
    V1459CohortTracker,
)


def _run(run_id, *, status, version="_codex_v1.4.59", config="cfg-a", armed_at_ms=10):
    return {
        "run_id": run_id,
        "symbol": "ETHUSDC",
        "status": status,
        "armed_at_ms": armed_at_ms,
        "params_json": json.dumps(
            {
                "mode": "adaptive_continuous",
                "symbol": "ETHUSDC",
                "adaptive": {
                    "session_id": f"operational-{run_id}",
                    "codex_v1_version": version,
                    "config_sha": config,
                    "canary_contract": "canary-a",
                },
            }
        ),
    }


class FakeDb:
    def __init__(self, runs, reconciliations):
        self.runs = runs
        self.reconciliations = reconciliations

    async def fetchall(self, sql, params=()):
        if "FROM mainnet_runs" in sql:
            return self.runs
        if "FROM run_reconciliations" in sql:
            return self.reconciliations
        raise AssertionError(sql)


class FakeConfigRepo:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value):
        self.values[key] = value


@pytest.mark.asyncio
async def test_tracker_adopts_exact_matching_history_and_uses_latest_complete_reconciliation():
    db = FakeDb(
        [
            _run("r1", status="COMPLETED", armed_at_ms=100),
            _run("r2", status="COMPLETED", armed_at_ms=200),
            _run("r3", status="ENTRY_EXPIRED", armed_at_ms=300),
            _run("r4", status="ARMED", armed_at_ms=400),
            _run("old-version", status="COMPLETED", version="_codex_v1.4.58"),
            _run("other-config", status="COMPLETED", config="cfg-b"),
        ],
        [
            {"run_id": "r1", "reconciliation_status": "COMPLETE", "net_pnl_usdc": 0.05},
            {"run_id": "r2", "reconciliation_status": "DATA_INCOMPLETE", "net_pnl_usdc": -0.10},
        ],
    )
    config = FakeConfigRepo()
    tracker = V1459CohortTracker(db=db, config_repo=config)

    session = await tracker.ensure_session(
        code_version="_codex_v1.4.59",
        config_sha="cfg-a",
        symbol="ETHUSDC",
        canary_contract="canary-a",
        target_paid_closed_fills=20,
        now_ms=500,
    )
    snapshot = await tracker.snapshot(session)

    assert session.started_at_ms == 100
    assert session.session_id == "v1459_cohort_500"
    assert TRACKING_CONFIG_KEY in config.values
    assert snapshot.attempts == 3
    assert snapshot.paid_closed_fills == 1
    assert snapshot.wins == 1
    assert snapshot.net_pnl_usdc == pytest.approx(0.05)
    assert snapshot.entry_expired == 1
    assert snapshot.active_runs == 1
    assert snapshot.unreconciled_completed == 1
    assert snapshot.operational_session_count == 4
    assert snapshot.wr_pct == pytest.approx(100.0)
    assert snapshot.ev_per_attempt_usdc == pytest.approx(0.05 / 3)


@pytest.mark.asyncio
async def test_tracker_reuses_the_same_definition_instead_of_resetting_on_restart():
    db = FakeDb([], [])
    config = FakeConfigRepo()
    tracker = V1459CohortTracker(db=db, config_repo=config)

    first = await tracker.ensure_session(
        code_version="_codex_v1.4.59",
        config_sha="cfg-a",
        symbol="ETHUSDC",
        canary_contract="canary-a",
        target_paid_closed_fills=20,
        now_ms=500,
    )
    second = await tracker.ensure_session(
        code_version="_codex_v1.4.59",
        config_sha="cfg-a",
        symbol="ETHUSDC",
        canary_contract="canary-a",
        target_paid_closed_fills=20,
        now_ms=900,
    )

    assert second == first
    assert json.loads(config.values[TRACKING_CONFIG_KEY])["session_id"] == "v1459_cohort_500"
