from __future__ import annotations

import json

import pytest

from src.gridbot.mainnet.runtime_identity import RuntimeIdentity
from src.gridbot.mainnet.v1459_observation_contract import (
    ObservationContractError,
    V1459ObservationFlags,
)
from src.gridbot.mainnet.v1459_observation_coordinator import (
    V1459ObservationCoordinator,
)
from src.gridbot.mainnet.v1459_observation_runtime import (
    V1459ObservationRuntime,
    V1459RuntimeContext,
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
        self.opportunities: dict[tuple[str, str], dict] = {}

    async def upsert_session(self, payload: dict) -> bool:
        self.sessions.append(dict(payload))
        return True

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
    async def record_shadow_evaluation(self, payload: dict) -> bool:
        return True

    async def record_reconciliation(self, payload: dict, *, trades=(), incomes=()) -> bool:
        return True


def _runtime(flags: V1459ObservationFlags, *, observed=None):
    evidence = _EvidenceRepo()
    coordinator = V1459ObservationCoordinator(
        flags=flags,
        evidence_repo=evidence,
        result_repo=_ResultRepo(),
    )
    runtime = V1459ObservationRuntime(
        coordinator=coordinator,
        context=V1459RuntimeContext(
            expected_identity=_identity(),
            observed_identity=observed or _identity(),
            code_version="v1.4.59",
        ),
    )
    return runtime, evidence


def _session():
    return {
        "session_id": "session-a",
        "started_at_ms": 1_000,
        "terminal_runs": 2,
        "net_pnl_usdc": 0.1,
        "high_water_net_pnl_usdc": 0.2,
        "rearm_enabled": True,
        "disabled_states": {"falling"},
        "counters": {
            "gross_pnl_usdc": 0.3,
            "commission_usdc": 0.2,
            "route_state_action_pnl": {"lane|state|action": {"runs": 2}},
        },
    }


@pytest.mark.asyncio
async def test_disabled_runtime_is_a_noop_and_has_no_order_capabilities() -> None:
    runtime, evidence = _runtime(V1459ObservationFlags())
    result = await runtime.checkpoint_session(_session(), checkpoint_at_ms=2_000)
    assert result.status == "DISABLED"
    assert evidence.sessions == []
    assert runtime.permits_order_mutation is False
    for name in ("create_order", "place_order", "cancel_order", "amend_order"):
        assert not hasattr(runtime, name)


@pytest.mark.asyncio
async def test_session_revisions_increment_only_for_attempted_writes() -> None:
    flags = V1459ObservationFlags(enabled=True, persist_session=True)
    runtime, evidence = _runtime(flags)
    await runtime.checkpoint_session(_session(), checkpoint_at_ms=2_000)
    await runtime.checkpoint_session(_session(), checkpoint_at_ms=3_000)
    assert [row["revision"] for row in evidence.sessions] == [0, 1]
    assert evidence.sessions[-1]["route_stats"] == {
        "lane|state|action": {"runs": 2}
    }


@pytest.mark.asyncio
async def test_identity_mismatch_is_persisted_paused_with_rearm_cleared() -> None:
    flags = V1459ObservationFlags(enabled=True, persist_session=True)
    runtime, evidence = _runtime(flags, observed=_identity(config_hash="wrong"))
    result = await runtime.checkpoint_session(_session(), checkpoint_at_ms=2_000)
    assert result.status == "PAUSED_REQUIRES_ACK"
    assert result.reason == "config_hash_mismatch"
    assert evidence.sessions[-1]["rearm_pending"] is False


@pytest.mark.asyncio
async def test_accepted_and_blocked_opportunities_are_deduplicated() -> None:
    flags = V1459ObservationFlags(enabled=True, record_opportunities=True)
    runtime, evidence = _runtime(flags)
    kwargs = {
        "session_id": "session-a",
        "decision_payload": {
            "opportunity_id": "opp-a",
            "lane_code": "STUP-S",
            "market_state": "clean_extension",
            "promotion_source": None,
            "live_effective_action": {"entry": "E2"},
        },
        "raw_decision": {"accepted": False, "reason": "soft_gate"},
        "effective_decision": {"accepted": False},
        "observed_at_ms": 2_000,
        "decision_at_ms": 1_999,
        "source_run_id": "run-a",
        "opportunity_bucket": 33,
        "symbol": "ETHUSDC",
        "side": "SHORT",
        "features": {"b": 2, "a": 1},
        "feature_timestamps": {"a": 1_998, "b": 1_998},
    }
    first = await runtime.record_opportunity(**kwargs)
    retry = await runtime.record_opportunity(**kwargs)
    assert first.status == "BLOCKED_OBSERVED" and first.inserted
    assert retry.status == "BLOCKED_OBSERVED" and not retry.inserted
    assert len(evidence.opportunities) == 1
    row = evidence.opportunities[("session-a", "opp-a")]
    assert len(row["feature_hash"]) == 64
    assert row["source_run_id"] == "run-a"
    assert row["opportunity_bucket"] == 33
    assert row["decision_at_ms"] == 1_999
    assert row["evidence_contract_version"] == "v1459-opportunity-evidence-v2"
    assert row["outcome_blind"] is True
    assert json.loads(row["feature_snapshot_json"]) == {"a": 1, "b": 2}
    assert json.loads(row["feature_timestamps_json"]) == {"a": 1_998, "b": 1_998}

    future_feature = dict(
        kwargs,
        feature_timestamps={"a": 2_001},
    )
    with pytest.raises(ObservationContractError, match="feature timestamp cannot follow"):
        await runtime.record_opportunity(**future_feature)

    accepted = dict(kwargs)
    accepted["decision_payload"] = dict(
        kwargs["decision_payload"], opportunity_id="opp-b"
    )
    accepted["effective_decision"] = {"accepted": True}
    assert (await runtime.record_opportunity(**accepted)).status == "ACCEPTED_OBSERVED"

