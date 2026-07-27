from dataclasses import replace

import pytest

from src.gridbot.strategy.codex_adaptive_controller import (
    AdaptiveControllerConfig,
    AdaptiveControllerInput,
    AdaptiveRoute,
    CodexAdaptiveController,
    DecisionMode,
    ExecutionQuality,
    ExecutorAction,
    build_opportunity_id,
    decide_adaptive_route,
    deterministic_config_sha256,
)


def _request(**overrides):
    values = {
        "adaptive_session_id": "session-20260712",
        "symbol": "ETHUSDC",
        "lane_code": "STUP-S",
        "market_state": "STUP-S:clean_extension",
        "side": "LONG",
        "opportunity_bucket": 1_782_000_000,
        "execution_quality": ExecutionQuality.EXECUTABLE,
        "incumbent_accepted": True,
        "incumbent_action": ExecutorAction("L_E0_TP10_SL6_T45", 10.0, "long_tp10_profile"),
    }
    values.update(overrides)
    return AdaptiveControllerInput(**values)


def test_clean_extension_tp14_is_blocked_with_no_selected_action():
    decision = decide_adaptive_route(
        _request(incumbent_action=ExecutorAction("L_E0_TP14_SL8_T90", 14.0, "tp14_profile"))
    )

    assert decision.incumbent_route is AdaptiveRoute.BLOCK
    assert decision.challenger_route is AdaptiveRoute.BLOCK
    assert decision.selected_route is AdaptiveRoute.BLOCK
    assert decision.selected_action is None
    assert decision.stop_reason == "stups_clean_extension_tp14_loss_guard"


@pytest.mark.parametrize("tp_bp", [8.0, 10.0])
def test_clean_extension_tp8_tp10_is_thin_scalp(tp_bp):
    action = ExecutorAction(f"L_E0_TP{tp_bp:g}_SL6_T45", tp_bp, "unchanged_executor_profile")

    decision = decide_adaptive_route(_request(incumbent_action=action))

    assert decision.selected_route is AdaptiveRoute.THIN_SCALP
    assert decision.selected_action is action
    assert decision.stop_reason == "stups_clean_extension_tp8_tp10_gate_pass"


def test_recovery_execution_quality_is_always_observe_only():
    decision = decide_adaptive_route(
        _request(
            lane_code="CNL-WPR-L",
            market_state="CNL-WPR-L:discount_mixed",
            execution_quality=ExecutionQuality.RECOVERY,
            challenger_route=AdaptiveRoute.NORMAL,
        ),
        AdaptiveControllerConfig(challenger_enabled=True),
    )

    assert decision.selected_route is AdaptiveRoute.OBSERVE_ONLY
    assert decision.selected_action is None
    assert decision.stop_reason == "recovery_observe_only"


@pytest.mark.parametrize(
    "market_state",
    [
        "CNL-WPR-L:discount_mixed",
        "CNL-WPR-L:discount_delayed_reclaim",
        "CNL-WPR-L:falling_continuation_probe",
        "CNL-WPR-L:missing_features",
    ],
)
def test_base_cnl_routes_stay_live_when_execution_quality_is_executable(market_state):
    action = ExecutorAction("L_E0_TP12_SL6_T45", 12.0, "cnl_base_executor_profile")

    decision = decide_adaptive_route(
        _request(
            lane_code="CNL-WPR-L",
            market_state=market_state,
            incumbent_action=action,
            execution_quality=ExecutionQuality.EXECUTABLE,
        )
    )

    assert decision.selected_route is AdaptiveRoute.NORMAL
    assert decision.selected_action is action
    assert decision.stop_reason == "default_live_profile"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"market_state": "NEW-STATE"}, "unknown_market_state"),
        ({"execution_quality": "UNTRUSTED"}, "invalid_execution_quality"),
        ({"incumbent_action": ExecutorAction("bad", float("nan"), "profile")}, "invalid_incumbent_action"),
        ({"adaptive_session_id": ""}, "missing_required_data"),
    ],
)
def test_invalid_required_data_fails_closed(overrides, reason):
    decision = decide_adaptive_route(_request(**overrides))

    assert decision.selected_route is AdaptiveRoute.BLOCK
    assert decision.selected_action is None
    assert decision.decision_mode is DecisionMode.FAIL_CLOSED
    assert decision.stop_reason == reason


def test_challenger_can_only_reduce_route_and_never_changes_executor_profile():
    action = ExecutorAction("L_E0_TP12_SL6_T45", 12.0, "must_not_change")
    controller = CodexAdaptiveController(AdaptiveControllerConfig(challenger_enabled=True))

    selected = controller.decide(_request(incumbent_action=action, challenger_route=AdaptiveRoute.THIN_SCALP))
    cannot_relax = controller.decide(_request(challenger_route=AdaptiveRoute.NORMAL))

    assert selected.incumbent_route is AdaptiveRoute.NORMAL
    assert selected.challenger_route is AdaptiveRoute.THIN_SCALP
    assert selected.selected_route is AdaptiveRoute.THIN_SCALP
    assert selected.decision_mode is DecisionMode.CHALLENGER_ROUTE
    assert selected.incumbent_action is action
    assert selected.challenger_action is action
    assert selected.selected_action is action
    assert cannot_relax.selected_route is AdaptiveRoute.THIN_SCALP
    assert cannot_relax.challenger_action is cannot_relax.incumbent_action


def test_hash_and_opportunity_helpers_are_deterministic_and_sensitive_to_inputs():
    config = AdaptiveControllerConfig(challenger_enabled=True)
    same_config = replace(config)
    first = build_opportunity_id(
        symbol="ETHUSDC",
        lane_code="STUP-S",
        market_state="STUP-S:clean_extension",
        side="LONG",
        action_id="L_E0_TP10_SL6_T45",
        opportunity_bucket=123,
    )
    second = build_opportunity_id(
        symbol="ETHUSDC",
        lane_code="STUP-S",
        market_state="STUP-S:clean_extension",
        side="LONG",
        action_id="L_E0_TP10_SL6_T45",
        opportunity_bucket=123,
    )
    changed = build_opportunity_id(
        symbol="ETHUSDC",
        lane_code="STUP-S",
        market_state="STUP-S:clean_extension",
        side="LONG",
        action_id="L_E0_TP14_SL8_T90",
        opportunity_bucket=123,
    )

    assert deterministic_config_sha256(config) == deterministic_config_sha256(same_config)
    assert first == second
    assert first != changed
    assert first.startswith("opp_")


def test_envelope_contains_stable_audit_fields():
    controller = CodexAdaptiveController()
    decision = controller.decide(_request())

    assert decision.adaptive_session_id == "session-20260712"
    assert decision.policy_version == controller.config.policy_version
    assert decision.config_sha256 == controller.config_sha256
    assert decision.opportunity_id.startswith("opp_")
    assert decision.market_state == "STUP-S:clean_extension"
    assert decision.execution_quality is ExecutionQuality.EXECUTABLE

@pytest.mark.parametrize(
    ("lane_code", "market_state"),
    [
        ("W1D", "W1D:mixed"),
        ("S1P-L", "S1P-L:ordinary_pullback_pre_vwap"),
        ("SFD-S", "SFD-S:strong_down_continuation"),
    ],
)
def test_current_live_states_are_known_incumbent_routes(lane_code, market_state):
    decision = decide_adaptive_route(_request(lane_code=lane_code, market_state=market_state))

    assert decision.incumbent_route is AdaptiveRoute.NORMAL
    assert decision.decision_mode.value != "FAIL_CLOSED"