import pytest

from config.settings import Settings
from src.gridbot.mainnet.tp_policy_shadow import (
    build_active_sample,
    build_outcomes,
    policy_definitions,
    baseline_snapshot,
    baseline_snapshot_from_order_plan,
    validate_policy,
)
from src.gridbot.strategy.long_pullback import Candle


def _settings(**overrides):
    data = {
        "binance_api_key": "key",
        "binance_api_secret": "secret",
        "telegram_chat_id": "123",
        "mainnet_api_key": "main-key",
        "mainnet_api_secret": "main-secret",
        "mainnet_symbol": "ETHUSDC",
        "mainnet_require_zero_maker_fee": False,
    }
    data.update(overrides)
    return Settings(**data)


def _sample(**overrides):
    data = {
        "sample_id": "sample_tp",
        "run_id": "run_tp",
        "opportunity_id": "opp_tp",
        "symbol": "ETHUSDC",
        "shadow_lane_family": "W1D",
        "candidate_lane": "W1D",
        "shadow_lane": None,
        "side": "LONG",
        "strategy": "S1_BB_RSI",
        "start_ms": 0,
        "entry_price": 100.0,
        "tp_price": 101.0,
        "sl_price": 99.0,
        "fill_model": "immediate_shadow",
        "entry_ttl_s": 0,
        "requested_notional_usdc": 200.0,
        "features": {"maker_fee_bp": 0.0, "taker_fee_bp": 0.0},
    }
    data.update(overrides)
    return data


def _events_by_policy(events):
    return {event["tp_policy_id"]: event for event in events}


def test_v132_tp_policy_invariants_and_mid_none():
    settings = _settings()
    baseline = baseline_snapshot(settings, _sample())
    assert baseline is not None
    policies = policy_definitions(settings, baseline)
    for policy in policies:
        validate_policy(policy)
        qty_sum = sum(float(policy.get(field) or 0.0) for field in ("tp1_qty_frac", "mid_qty_frac", "full_tp_qty_frac", "runner_qty_frac"))
        assert qty_sum == pytest.approx(1.0)
    profit_a = next(policy for policy in policies if policy["tp_policy_id"] == "profit_a_runner40")
    mid_restore = next(policy for policy in policies if policy["tp_policy_id"] == "mid_restore_8bp")
    assert profit_a["mid_tp_bp"] is None
    assert profit_a["runner_qty_frac"] == pytest.approx(0.40)
    assert mid_restore["mid_tp_bp"] == pytest.approx(8.0)


def test_v132_baseline_snapshot_is_stable_after_config_change():
    active = build_active_sample(_settings(mainnet_partial_exit_pct=0.40), _sample(), source_type="shadow_sample")
    assert active is not None
    later_baseline = baseline_snapshot(_settings(mainnet_partial_exit_pct=0.70), active)
    assert later_baseline is not None
    assert active["baseline_tp1_qty_frac"] == pytest.approx(0.40)
    assert active["baseline_order_plan_hash"] != later_baseline["baseline_order_plan_hash"]

    events = build_outcomes(
        _settings(mainnet_partial_exit_pct=0.70),
        active,
        [Candle(0, 100.0, 100.06, 100.0, 100.04, 1.0), Candle(60_000, 100.04, 101.0, 100.7, 100.9, 1.0)],
    )
    assert events is not None
    baseline_event = _events_by_policy(events)["baseline"]
    assert baseline_event["baseline_tp1_qty_frac"] == pytest.approx(0.40)
    assert baseline_event["baseline_order_plan_hash"] == active["baseline_order_plan_hash"]


def test_v132_actual_order_plan_overrides_settings_baseline():
    sample = _sample(sample_id="sample_live_plan")
    settings = _settings(mainnet_partial_exit_pct=0.70, mainnet_mid_exit_pct=0.80)
    baseline = baseline_snapshot_from_order_plan(
        settings,
        sample,
        current_qty=1.0,
        orders=[("run_tp_tp1", "0.4", 100.05), ("run_tp_tp3", "0.3", 101.0)],
    )
    assert baseline is not None
    active = build_active_sample(settings, sample, source_type="live_trade", baseline_override=baseline)
    assert active is not None
    assert active["baseline_tp1_qty_frac"] == pytest.approx(0.40)
    assert active["baseline_full_tp_qty_frac"] == pytest.approx(0.30)
    assert active["baseline_runner_qty_frac"] == pytest.approx(0.30)


def test_v132_paired_delta_profit_a_runner40_beats_baseline_on_same_path():
    settings = _settings(mainnet_trail_arm_frac=0.7, mainnet_trail_giveback_frac=0.25, mainnet_trail_profit_floor_bp=1.5)
    active = build_active_sample(settings, _sample(), source_type="shadow_sample")
    assert active is not None
    events = build_outcomes(
        settings,
        active,
        [
            Candle(0, 100.0, 100.06, 100.0, 100.04, 1.0),
            Candle(60_000, 100.04, 101.20, 100.80, 101.0, 1.0),
        ],
    )
    assert events is not None
    by_policy = _events_by_policy(events)
    assert by_policy["baseline"]["delta_vs_baseline_bp_after_fee"] == pytest.approx(0.0)
    assert by_policy["profit_a_runner40"]["delta_vs_baseline_bp_after_fee"] == pytest.approx(3.75)
    assert by_policy["profit_a_runner40"]["beats_baseline"] is True
    assert by_policy["profit_a_runner40"]["tp1_touch_mismatch_count"] == 0


def test_v132_sl_before_tp1_exits_all_components_at_sl_with_zero_delta():
    settings = _settings()
    active = build_active_sample(settings, _sample(sl_price=99.5), source_type="shadow_sample")
    assert active is not None
    events = build_outcomes(settings, active, [Candle(0, 100.0, 100.03, 99.40, 99.6, 1.0)])
    assert events is not None
    by_policy = _events_by_policy(events)
    for event in events:
        assert event["path_end_reason"] == "sl_before_tp1"
        assert event["tp1_touched"] is False
        assert event["delta_vs_baseline_bp_after_fee"] == pytest.approx(0.0)
        assert sum(row["qty_frac"] for row in event["component_exits"]) == pytest.approx(1.0)
    assert by_policy["profit_b_runner45"]["beats_baseline"] is False


def test_v132_ambiguous_pre_tp1_excluded_from_primary_but_tp1_invariant_ok():
    settings = _settings()
    active = build_active_sample(settings, _sample(sl_price=99.5), source_type="shadow_sample")
    assert active is not None
    events = build_outcomes(settings, active, [Candle(0, 100.0, 100.08, 99.40, 100.0, 1.0)])
    assert events is not None
    for event in events:
        assert event["ambiguous_path"] is True
        assert event["ambiguous_stage"] == "pre_tp1"
        assert event["primary_promotion_eligible"] is False
        assert event["stress_mode_result"] == "adverse_first"
        assert event["tp1_touch_mismatch_count"] == 0



def test_v133_force_terminalizes_live_path_before_policy_ttl_and_reports_drift():
    settings = _settings(mainnet_trail_arm_frac=0.7, mainnet_trail_giveback_frac=0.25)
    active = build_active_sample(
        settings,
        _sample(sample_id="live_force", run_id="run_force"),
        source_type="live_trade",
        actual_live_pnl_bp_after_fee=2.0,
    )
    assert active is not None
    candles = [
        Candle(0, 100.0, 100.06, 100.0, 100.04, 1.0),
        Candle(60_000, 100.04, 100.10, 100.02, 100.08, 1.0),
    ]

    assert build_outcomes(settings, active, candles) is None
    events = build_outcomes(
        settings,
        active,
        candles,
        force_terminal=True,
        terminal_reason="terminalized_from_live_run",
    )

    assert events is not None
    baseline = _events_by_policy(events)["baseline"]
    assert baseline["path_end_reason"] == "terminalized_from_live_run"
    assert baseline["baseline_simulator_drift_bp"] == pytest.approx(
        baseline["baseline_pnl_bp_after_fee"] - 2.0
    )

def test_v132_no_fill_emits_policy_outcomes_instead_of_silent_drop():
    settings = _settings()
    active = build_active_sample(
        settings,
        _sample(sample_id="sample_no_fill", fill_model="limit_touch", entry_ttl_s=60, entry_price=100.0),
        source_type="shadow_sample",
    )
    assert active is not None
    events = build_outcomes(settings, active, [Candle(120_000, 101.0, 101.4, 100.8, 101.2, 1.0)])
    assert events is not None
    assert events
    for event in events:
        assert event["filled"] is False
        assert event["path_end_reason"] == "no_fill"
        assert event["primary_promotion_eligible"] is False
        assert event["delta_vs_baseline_bp_after_fee"] == pytest.approx(0.0)



def test_v134_tp_policy_missing_entry_ttl_falls_back_to_180_seconds():
    settings = _settings()
    active = build_active_sample(
        settings,
        _sample(sample_id="sample_default_ttl", fill_model="limit_touch", entry_ttl_s=None, entry_price=100.0),
        source_type="shadow_sample",
    )
    assert active is not None
    assert build_outcomes(settings, active, [Candle(60_000, 101.0, 101.4, 100.8, 101.2, 1.0)]) is None

    assert build_outcomes(
        settings,
        active,
        [
            Candle(60_000, 101.0, 101.4, 100.8, 101.2, 1.0),
            Candle(120_000, 101.0, 101.4, 100.8, 101.2, 1.0),
        ],
    ) is None

    events = build_outcomes(
        settings,
        active,
        [
            Candle(60_000, 101.0, 101.4, 100.8, 101.2, 1.0),
            Candle(120_000, 101.0, 101.4, 100.8, 101.2, 1.0),
            Candle(180_000, 101.0, 101.4, 100.8, 101.2, 1.0),
        ],
    )
    assert events is not None
    assert events
    assert {event["path_end_reason"] for event in events} == {"no_fill"}
def test_v132_post_tp1_trail_vs_sl_same_bar_is_ambiguous():
    settings = _settings(mainnet_trail_arm_frac=0.7, mainnet_trail_giveback_frac=0.25, mainnet_trail_profit_floor_bp=0.0)
    active = build_active_sample(settings, _sample(sample_id="sample_trail_amb", sl_price=99.5), source_type="shadow_sample")
    assert active is not None
    events = build_outcomes(
        settings,
        active,
        [
            Candle(0, 100.0, 100.06, 100.0, 100.05, 1.0),
            Candle(60_000, 100.05, 101.20, 99.40, 100.1, 1.0),
        ],
    )
    assert events is not None
    profit_a = _events_by_policy(events)["profit_a_runner40"]
    assert profit_a["ambiguous_path"] is True
    assert profit_a["ambiguous_stage"] == "trail_vs_sl"
    assert profit_a["primary_promotion_eligible"] is False
    assert profit_a["path_end_reason"] == "ambiguous_terminal"


def test_v132_same_bar_runner_arm_and_trail_is_ambiguous_without_sl():
    settings = _settings(mainnet_trail_arm_frac=0.7, mainnet_trail_giveback_frac=0.25, mainnet_trail_profit_floor_bp=0.0)
    active = build_active_sample(settings, _sample(sample_id="sample_trail_same_bar", sl_price=99.5), source_type="shadow_sample")
    assert active is not None
    events = build_outcomes(
        settings,
        active,
        [
            Candle(0, 100.0, 100.06, 100.0, 100.05, 1.0),
            Candle(60_000, 100.05, 101.20, 100.85, 101.0, 1.0),
        ],
    )
    assert events is not None
    profit_a = _events_by_policy(events)["profit_a_runner40"]
    assert profit_a["ambiguous_path"] is True
    assert profit_a["ambiguous_stage"] == "trail_same_bar_arm"
    assert profit_a["primary_promotion_eligible"] is False
    assert profit_a["stress_mode_result"] == "adverse_first"
    assert profit_a["runner_exit_type"] == "trail"