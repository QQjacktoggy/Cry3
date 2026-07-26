from dataclasses import replace

import pytest

from src.gridbot.mainnet.v1459_regime_runtime import (
    RANGE_SCALP,
    TREND_RUNNER,
    V1459RegimeConfig,
    V1459RegimeRuntime,
    V1459RegimeState,
    apply_v1459_regime_overlay,
    map_market_state,
)
from src.gridbot.strategy.codex_v1_live import CodexV1Decision


def _decision() -> CodexV1Decision:
    return CodexV1Decision(
        accepted=True,
        version="_codex_v1.test",
        baseline="unit",
        lane="lane",
        lane_code="W1A",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=9.0,
        size_mult=0.4,
        notional_mult=0.4,
        requested_notional_usdc=20.0,
        reason="incumbent",
        regime="incumbent",
        metrics={"market_state": "STUP-S:mixed", "incumbent": True},
        policy_tag="incumbent-policy",
    )


def test_detailed_market_state_mapping_is_bounded():
    assert map_market_state("STUP-S:clean_extension") is V1459RegimeState.TREND_UP
    assert map_market_state("CNL-WPR-L:fast_reclaim") is V1459RegimeState.TREND_UP
    assert map_market_state("SFD-S:strong_down_continuation") is V1459RegimeState.TREND_DOWN
    assert (
        map_market_state("CNL-WPR-L:falling_continuation_probe")
        is V1459RegimeState.TREND_DOWN
    )
    assert map_market_state("CNL-WPR-L:discount_mixed") is V1459RegimeState.RANGE
    assert (
        map_market_state("CNL-WPR-L:discount_delayed_reclaim")
        is V1459RegimeState.RANGE
    )
    assert map_market_state("STUP-S:counter_recoil") is V1459RegimeState.SHOCK
    assert (
        map_market_state("CNL-WPR-L:falling_discount_trap")
        is V1459RegimeState.SHOCK
    )
    assert map_market_state("future:unrecognized_state") is V1459RegimeState.UNCERTAIN


def test_regime_config_uses_ninety_second_stale_default():
    config = V1459RegimeConfig()

    assert config.stale_after_ms == 90_000
    assert config.fsm_config().stale_after_ms == 90_000


def test_two_confirmations_and_minimum_dwell_prevent_flapping():
    runtime = V1459RegimeRuntime()
    first = runtime.evaluate("STUP-S:clean_extension", decision_time_ms=0)
    assert first.state is V1459RegimeState.UNCERTAIN
    second = runtime.evaluate("STUP-S:weak_chop", decision_time_ms=15_000)
    assert second.state is V1459RegimeState.UNCERTAIN
    third = runtime.evaluate("STUP-S:clean_extension", decision_time_ms=30_000)
    assert third.state is V1459RegimeState.UNCERTAIN
    fourth = runtime.evaluate("STUP-S:clean_extension", decision_time_ms=45_000)
    assert fourth.state is V1459RegimeState.TREND_UP


def test_shock_is_immediate():
    runtime = V1459RegimeRuntime()
    runtime.evaluate("STUP-S:clean_extension", decision_time_ms=0)
    shock = runtime.evaluate("STUP-S:counter_recoil", decision_time_ms=1_000)
    assert shock.state is V1459RegimeState.SHOCK


def test_uncertain_is_immediate_invalid_fallback():
    runtime = V1459RegimeRuntime()
    runtime.evaluate("STUP-S:clean_extension", decision_time_ms=0)
    uncertain = runtime.evaluate("unknown:bad_state", decision_time_ms=1_000)
    assert uncertain.state is V1459RegimeState.UNCERTAIN
    assert uncertain.profile.name == "INCUMBENT_FALLBACK"


def test_candidate_only_only_adds_audit_metrics():
    runtime = V1459RegimeRuntime()
    runtime.evaluate("STUP-S:clean_extension", decision_time_ms=0)
    overlay = runtime.evaluate("STUP-S:clean_extension", decision_time_ms=15_000)
    baseline = _decision()
    candidate = apply_v1459_regime_overlay(baseline, overlay)
    assert candidate.entry_offset_bp == baseline.entry_offset_bp
    assert candidate.size_mult == baseline.size_mult
    assert candidate.notional_mult == baseline.notional_mult
    assert candidate.requested_notional_usdc == baseline.requested_notional_usdc
    assert candidate.metrics["v1459_regime_profile"] == "TREND_RUNNER"
    assert candidate.metrics["incumbent"] is True


def test_uncertain_enforcement_is_exact_incumbent_except_audit():
    runtime = V1459RegimeRuntime()
    overlay = runtime.evaluate("unknown:bad_state", decision_time_ms=0)
    baseline = _decision()
    result = apply_v1459_regime_overlay(baseline, overlay, mode="enforcement")
    assert replace(result, metrics=baseline.metrics) == baseline
    assert result.metrics["v1459_regime_state"] == "UNCERTAIN"

@pytest.mark.parametrize(
    "lane_code",
    (
        "ANCHOR-L", "ANCHOR-S", "RP1", "W2A", "W5A", "W3A", "W1A",
        "W6A", "W5B", "W4A", "W7A", "W1B", "W3B", "W6B", "W2B",
        "W4B", "W1C", "W6C", "W1D", "S1P-L", "W2C", "W3C", "W1E",
        "HUE-L", "STUP-S", "CNL-WPR-L", "SFD-S",
    ),
)
def test_all_bare_lanes_enforce_incumbent_fallback(lane_code):
    assert map_market_state(lane_code) is V1459RegimeState.UNCERTAIN
    overlay = V1459RegimeRuntime().evaluate(lane_code, decision_time_ms=0)
    baseline = replace(_decision(), lane_code=lane_code)
    result = apply_v1459_regime_overlay(baseline, overlay, mode="enforcement")

    assert replace(result, metrics=baseline.metrics) == baseline
    assert result.metrics["v1459_regime_state"] == "UNCERTAIN"
    assert result.metrics["v1459_regime_profile"] == "INCUMBENT_FALLBACK"


def test_enforced_trend_and_range_profiles_change_actions_differently():
    trend_runtime = V1459RegimeRuntime()
    trend_runtime.evaluate("STUP-S:clean_extension", decision_time_ms=0)
    trend = trend_runtime.evaluate("STUP-S:clean_extension", decision_time_ms=15_000)
    range_runtime = V1459RegimeRuntime()
    range_runtime.evaluate("STUP-S:weak_chop", decision_time_ms=0)
    range_ = range_runtime.evaluate("STUP-S:weak_chop", decision_time_ms=15_000)

    trend_result = apply_v1459_regime_overlay(_decision(), trend, mode="enforcement")
    range_result = apply_v1459_regime_overlay(_decision(), range_, mode="enforcement")
    assert trend.profile == TREND_RUNNER
    assert range_.profile == RANGE_SCALP
    assert trend_result.requested_notional_usdc == 50.0
    assert range_result.requested_notional_usdc == 37.5
    assert trend_result.metrics["full_tp_bp"] == 16.0
    assert range_result.metrics["full_tp_bp"] == 8.0
    assert trend_result.metrics["maker_mode"] == "ONE_STEP_REPRICE"
    assert range_result.metrics["maker_mode"] == "PASSIVE"
