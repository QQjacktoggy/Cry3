from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from src.gridbot.mainnet.v1469_adaptive_identity import (
    EXECUTION_PROFILE_SCHEMA,
    MARKET_STATE_SCHEMA,
    RISK_POLICY_SCHEMA,
    BreakevenPolicy,
    DcaLayer,
    DcaPolicy,
    EarlyFailPolicy,
    ExecutionProfile,
    MarketStateIdentity,
    RepricePolicy,
    RiskPolicy,
    RunnerPolicy,
    TakeProfitLevel,
    TrailPolicy,
    canonical_json,
    canonicalize_execution_profile,
    execution_profile_hash,
    market_state_hash,
    risk_policy_hash,
)


def _execution_payload() -> dict:
    return {
        "schema": EXECUTION_PROFILE_SCHEMA,
        "profile_id": "TREND_PARTIAL",
        "entry_offset_bp": 2.0,
        "entry_ttl_s": 120,
        "maker_mode": "POST_ONLY",
        "reprice": {
            "enabled": True,
            "after_s": 45,
            "offset_bp": 1.0,
            "max_reprices": 1,
        },
        "take_profits": [
            {"level_id": "TP1", "target_bp": 6.0, "fraction": 0.7},
            {"level_id": "FULL", "target_bp": 16.0, "fraction": 0.3},
        ],
        "sl_bp": 10.0,
        "breakeven": {
            "enabled": True,
            "trigger_bp": 5.0,
            "lock_bp": 1.0,
        },
        "max_hold_s": 600,
        "trail": {
            "enabled": True,
            "arm_bp": 8.0,
            "giveback_bp": 3.0,
            "floor_bp": 2.0,
        },
        "runner": {
            "enabled": True,
            "fraction": 0.3,
            "take_profit_cap_bp": 16.0,
        },
        "early_fail": {
            "enabled": True,
            "after_s": 90,
            "max_mfe_bp": 2.0,
            "adverse_bp": 5.0,
        },
        "dca": {
            "enabled": True,
            "layers": [
                {
                    "layer_id": "DCA1",
                    "trigger_adverse_bp": 4.0,
                    "additional_fraction": 0.25,
                }
            ],
        },
    }


def _risk_payload() -> dict:
    return {
        "schema": RISK_POLICY_SCHEMA,
        "policy_id": "PROBATION_25",
        "paid_notional_cap_usdc": 25.0,
        "per_trade_loss_cap_usdc": 2.0,
        "lane_open_notional_cap_usdc": 50.0,
        "global_open_notional_cap_usdc": 100.0,
        "daily_soft_loss_cap_usdc": 5.0,
        "daily_hard_loss_cap_usdc": 8.0,
        "daily_profit_lock_trigger_usdc": 10.0,
        "daily_profit_lock_giveback_usdc": 4.0,
        "max_consecutive_losses": 2,
        "cooldown_s": 900,
    }


def test_market_state_identity_is_canonical_hashed_and_immutable() -> None:
    first = MarketStateIdentity(
        environment=" mainnet ",
        symbol="btcusdt",
        lane_code="w6a",
        effective_side="long",
        strategy="codex_v1",
        coarse_regime="trend_up",
        market_state="Clean-Extension",
    )
    second = MarketStateIdentity.from_mapping(
        {
            "schema": MARKET_STATE_SCHEMA,
            "environment": "MAINNET",
            "symbol": "BTCUSDT",
            "lane_code": "W6A",
            "effective_side": "LONG",
            "strategy": "CODEX_V1",
            "coarse_regime": "TREND_UP",
            "market_state": "clean_extension",
        }
    )

    assert first == second
    assert market_state_hash(first) == market_state_hash(second)
    assert len(first.identity_hash) == 64
    with pytest.raises(FrozenInstanceError):
        first.coarse_regime = "RANGE"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("effective_side", "FLAT"),
        ("coarse_regime", "SIDEWAYS"),
        ("market_state", ""),
    ],
)
def test_market_state_identity_fails_closed(field: str, value: str) -> None:
    payload = {
        "environment": "MAINNET",
        "symbol": "BTCUSDT",
        "lane_code": "W6A",
        "effective_side": "LONG",
        "strategy": "CODEX_V1",
        "coarse_regime": "RANGE",
        "market_state": "mixed",
    }
    payload[field] = value
    with pytest.raises(ValueError):
        MarketStateIdentity.from_mapping(payload)


def test_execution_profile_builds_immutable_nested_contract() -> None:
    profile = ExecutionProfile(
        profile_id="TREND_PARTIAL",
        entry_offset_bp=2,
        entry_ttl_s=120,
        maker_mode="post_only",
        reprice=RepricePolicy(
            enabled=True, after_s=45, offset_bp=1, max_reprices=1
        ),
        take_profits=(
            TakeProfitLevel(level_id="TP1", target_bp=6, fraction=0.7),
            TakeProfitLevel(level_id="FULL", target_bp=16, fraction=0.3),
        ),
        sl_bp=10,
        breakeven=BreakevenPolicy(enabled=True, trigger_bp=5, lock_bp=1),
        max_hold_s=600,
        trail=TrailPolicy(
            enabled=True, arm_bp=8, giveback_bp=3, floor_bp=2
        ),
        runner=RunnerPolicy(
            enabled=True, fraction=0.3, take_profit_cap_bp=16
        ),
        early_fail=EarlyFailPolicy(
            enabled=True, after_s=90, max_mfe_bp=2, adverse_bp=5
        ),
        dca=DcaPolicy(
            enabled=True,
            layers=(
                DcaLayer(
                    layer_id="DCA1",
                    trigger_adverse_bp=4,
                    additional_fraction=0.25,
                ),
            ),
        ),
    )

    assert profile.to_payload() == canonicalize_execution_profile(
        _execution_payload()
    )
    assert profile.profile_hash == execution_profile_hash(_execution_payload())
    with pytest.raises(FrozenInstanceError):
        profile.entry_ttl_s = 180  # type: ignore[misc]
    assert isinstance(profile.take_profits, tuple)
    assert isinstance(profile.dca.layers, tuple)


def _mutate_entry_offset(payload: dict) -> None:
    payload["entry_offset_bp"] = 3.0


def _mutate_entry_ttl(payload: dict) -> None:
    payload["entry_ttl_s"] = 150


def _mutate_maker(payload: dict) -> None:
    payload["maker_mode"] = "PASSIVE_LIMIT"


def _mutate_reprice(payload: dict) -> None:
    payload["reprice"]["after_s"] = 50


def _mutate_tp_target(payload: dict) -> None:
    payload["take_profits"][0]["target_bp"] = 7.0


def _mutate_tp_fraction(payload: dict) -> None:
    payload["take_profits"][0]["fraction"] = 0.6
    payload["take_profits"][1]["fraction"] = 0.4
    payload["runner"]["fraction"] = 0.4


def _mutate_sl(payload: dict) -> None:
    payload["sl_bp"] = 11.0


def _mutate_breakeven(payload: dict) -> None:
    payload["breakeven"]["lock_bp"] = 1.5


def _mutate_hold(payload: dict) -> None:
    payload["max_hold_s"] = 720


def _mutate_trail(payload: dict) -> None:
    payload["trail"]["giveback_bp"] = 4.0


def _mutate_runner(payload: dict) -> None:
    payload["runner"] = {
        "enabled": False,
        "fraction": 0.0,
        "take_profit_cap_bp": 0.0,
    }


def _mutate_early_fail(payload: dict) -> None:
    payload["early_fail"]["max_mfe_bp"] = 3.0


def _mutate_dca(payload: dict) -> None:
    payload["dca"]["layers"][0]["trigger_adverse_bp"] = 5.0


@pytest.mark.parametrize(
    "mutator",
    [
        _mutate_entry_offset,
        _mutate_entry_ttl,
        _mutate_maker,
        _mutate_reprice,
        _mutate_tp_target,
        _mutate_tp_fraction,
        _mutate_sl,
        _mutate_breakeven,
        _mutate_hold,
        _mutate_trail,
        _mutate_runner,
        _mutate_early_fail,
        _mutate_dca,
    ],
)
def test_every_execution_control_changes_profile_hash(mutator) -> None:
    baseline = _execution_payload()
    changed = deepcopy(baseline)
    mutator(changed)

    assert execution_profile_hash(changed) != execution_profile_hash(baseline)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entry_price", 118_234.5),
        ("entry_limit_price", 118_200.0),
        ("reference_price", 118_250.0),
        ("tp1_price", 118_310.0),
        ("full_tp_price", 118_440.0),
        ("sl_price", 118_100.0),
        ("timestamp_ms", 1_753_435_200_000),
        ("observed_at_ms", 1_753_435_201_000),
        ("decision_at_ms", 1_753_435_202_000),
        ("run_id", "run-2"),
        ("order_id", "order-2"),
        ("client_order_id", "client-order-2"),
        ("opportunity_id", "opportunity-2"),
        ("cap_tier", "PROBATION_25"),
        ("risk_policy_hash", "risk-hash-2"),
        ("paid_notional_cap_usdc", 25.0),
    ],
)
def test_absolute_prices_timestamps_and_ids_do_not_affect_profile_hash(
    field: str, value
) -> None:
    baseline = _execution_payload()
    decorated = deepcopy(baseline)
    decorated[field] = value

    assert execution_profile_hash(decorated) == execution_profile_hash(baseline)
    assert field not in canonicalize_execution_profile(decorated)


def test_risk_policy_hash_is_separate_from_execution_profile_hash() -> None:
    profile = _execution_payload()
    risk_25 = _risk_payload()
    risk_50 = deepcopy(risk_25)
    risk_50["policy_id"] = "LIVE_50"
    risk_50["paid_notional_cap_usdc"] = 50.0

    profile_hash_before = execution_profile_hash(profile)
    assert risk_policy_hash(risk_25) != risk_policy_hash(risk_50)
    assert execution_profile_hash(profile) == profile_hash_before
    assert "paid_notional_cap_usdc" not in canonicalize_execution_profile(profile)


def test_risk_policy_is_immutable_and_canonical() -> None:
    policy = RiskPolicy.from_mapping(_risk_payload())

    assert len(policy.policy_hash) == 64
    assert policy.policy_hash == risk_policy_hash(_risk_payload())
    with pytest.raises(FrozenInstanceError):
        policy.cooldown_s = 60  # type: ignore[misc]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row.__setitem__("entry_offset_bp", float("nan")),
        lambda row: row.__setitem__("entry_ttl_s", True),
        lambda row: row.__setitem__("unknown_execution_switch", True),
        lambda row: row["reprice"].__setitem__("enabled", "true"),
        lambda row: row["take_profits"][0].__setitem__("fraction", 0.8),
        lambda row: row["trail"].__setitem__("enabled", False),
        lambda row: row["dca"]["layers"][0].__setitem__(
            "additional_fraction", float("inf")
        ),
    ],
)
def test_execution_profile_validation_fails_closed(mutate) -> None:
    payload = _execution_payload()
    mutate(payload)
    with pytest.raises((TypeError, ValueError)):
        execution_profile_hash(payload)


def test_missing_execution_dimension_fails_closed() -> None:
    payload = _execution_payload()
    del payload["early_fail"]

    with pytest.raises(ValueError, match="missing required fields"):
        execution_profile_hash(payload)


def test_risk_policy_rejects_invalid_caps_and_unknown_fields() -> None:
    inverted = _risk_payload()
    inverted["daily_soft_loss_cap_usdc"] = 9.0
    with pytest.raises(ValueError, match="daily_hard_loss_cap_usdc"):
        risk_policy_hash(inverted)

    unknown = _risk_payload()
    unknown["mystery_cap"] = 1.0
    with pytest.raises(ValueError, match="unknown fields"):
        risk_policy_hash(unknown)


def test_canonical_json_is_deterministic_and_fail_closed() -> None:
    assert canonical_json({"z": 1.0000000001, "a": [2, 3.0]}) == (
        '{"a":[2,3.0],"z":1.0}'
    )
    with pytest.raises(ValueError):
        canonical_json({"value": float("nan")})
    with pytest.raises(TypeError):
        canonical_json({"value": {1, 2}})
    with pytest.raises(TypeError):
        canonical_json({1: "not-a-string-key"})  # type: ignore[dict-item]
