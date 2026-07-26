from __future__ import annotations

from dataclasses import replace

import pytest

from src.gridbot.mainnet.v1461_adaptive_gate import (
    AdaptiveActionMode,
    AdaptiveGateConfig,
    AdaptiveGateRiskInput,
    GateEvidence,
    RegimeCompatibility,
    apply_adaptive_gate_decision,
    promotion_token_id,
    select_adaptive_gate_decision,
)
from src.gridbot.strategy.codex_v1_live import CodexV1Decision


NOW = 100_000_000


def _evidence(**changes: object) -> GateEvidence:
    base = GateEvidence(
        opportunities=4,
        evaluable=4,
        tp_first=3,
        sl_first=1,
        net_pnl_usdc=0.04,
        last_outcome="tp1_first",
        last_outcome_at_ms=NOW - 1_000,
        matching_episode_count=1,
    )
    return replace(base, **changes)


def _select(**changes: object):
    values = {
        "incumbent_accepted": False,
        "promotion_eligible": True,
        "gate_family_id": "W6A_ENTRY_RISK",
        "lane": "W6A",
        "market_state": "TREND_UP",
        "episode_id": "ep-1",
        "compatibility": RegimeCompatibility.SUPPORTIVE,
        "evidence": _evidence(),
        "now_ms": NOW,
    }
    values.update(changes)
    return select_adaptive_gate_decision(**values)


def _codex(*, accepted: bool = True) -> CodexV1Decision:
    return CodexV1Decision(
        accepted=accepted,
        version="test",
        baseline="test",
        lane="W6A",
        lane_code="W6A",
        strategy="wildcat",
        side="LONG",
        entry_offset_bp=1.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0 if accepted else 0.0,
        reason="incumbent",
        metrics={"tp_bp": 7.0, "sl_bp": 15.0, "ttl_s": 60, "hold_s": 600},
    )


def test_supportive_recent_4_of_3_shadow_gets_one_fast_probe_token() -> None:
    result = _select()
    assert result.action_mode is AdaptiveActionMode.FAST_PROBE_0_5
    assert result.permits_order is True
    assert result.max_notional_usdc == 25.0
    assert result.token_id == promotion_token_id(
        result.policy_hash, "W6A_ENTRY_RISK", "W6A", "TREND_UP", "ep-1"
    )


def test_fast_probe_token_is_single_use() -> None:
    result = _select(token_consumed=True)
    assert result.action_mode is AdaptiveActionMode.SHADOW_BLOCK
    assert result.permits_order is False


def test_failed_probe_retries_only_in_a_new_episode() -> None:
    failed = _evidence(
        first_probe_net_pnl_usdc=-0.02,
        first_probe_episode_id="ep-1",
        paid_complete=1,
        paid_wins=0,
        paid_net_pnl_usdc=-0.02,
    )
    same_episode = _select(evidence=failed, token_consumed=True)
    next_episode = _select(evidence=failed, episode_id="ep-2")
    assert same_episode.action_mode is AdaptiveActionMode.SHADOW_BLOCK
    assert next_episode.action_mode is AdaptiveActionMode.FAST_PROBE_0_5
    assert next_episode.evidence_gate["retry_after_failed_probe"] is True
    assert next_episode.evidence_gate["paid_complete"] == 0


def test_successful_retry_sequence_can_reach_control() -> None:
    recovered = _evidence(
        opportunities=6,
        evaluable=6,
        tp_first=4,
        sl_first=2,
        matching_episode_count=2,
        first_probe_net_pnl_usdc=0.02,
        first_probe_episode_id="ep-2",
        paid_complete=3,
        paid_wins=2,
        paid_net_pnl_usdc=0.03,
    )
    assert (
        _select(evidence=recovered, episode_id="ep-2").action_mode
        is AdaptiveActionMode.CONTROL
    )


@pytest.mark.parametrize(
    "evidence",
    [
        _evidence(evaluable=3, tp_first=3, sl_first=0),
        _evidence(tp_first=2, sl_first=2),
        _evidence(net_pnl_usdc=0.0),
        _evidence(last_outcome="sl_first"),
        _evidence(incomplete=1),
        _evidence(last_outcome_at_ms=NOW - (6 * 60 * 60 + 1) * 1000),
    ],
)
def test_fast_probe_gate_is_closed(evidence: GateEvidence) -> None:
    assert _select(evidence=evidence).action_mode is AdaptiveActionMode.SHADOW_BLOCK


def test_paid_probe_and_6_of_4_enters_probation() -> None:
    evidence = _evidence(
        opportunities=6,
        evaluable=6,
        tp_first=4,
        sl_first=2,
        first_probe_net_pnl_usdc=0.0,
    )
    assert _select(evidence=evidence).action_mode is AdaptiveActionMode.PROBATION_0_5


def test_three_paid_two_wins_positive_restores_control() -> None:
    evidence = _evidence(
        opportunities=8,
        evaluable=6,
        tp_first=4,
        sl_first=2,
        first_probe_net_pnl_usdc=0.01,
        paid_complete=3,
        paid_wins=2,
        paid_net_pnl_usdc=0.03,
    )
    assert _select(evidence=evidence).action_mode is AdaptiveActionMode.CONTROL


def test_adverse_blocks_even_incumbent_accept() -> None:
    result = _select(
        incumbent_accepted=True,
        compatibility=RegimeCompatibility.ADVERSE,
    )
    assert result.action_mode is AdaptiveActionMode.SHADOW_BLOCK
    assert result.permits_order is False


def test_unknown_accept_is_half_probation_but_unknown_reject_stays_shadow() -> None:
    assert _select(
        incumbent_accepted=True,
        compatibility=RegimeCompatibility.UNKNOWN,
    ).action_mode is AdaptiveActionMode.PROBATION_0_5
    assert _select(
        incumbent_accepted=False,
        compatibility=RegimeCompatibility.UNKNOWN,
    ).action_mode is AdaptiveActionMode.SHADOW_BLOCK


def test_shock_and_integrity_are_not_overridden_by_positive_evidence() -> None:
    assert _select(
        compatibility=RegimeCompatibility.HARD_BLOCK,
    ).action_mode is AdaptiveActionMode.HARD_BLOCK
    assert _select(
        risk=AdaptiveGateRiskInput(integrity_safe=False),
    ).action_mode is AdaptiveActionMode.HALT


def test_lane_and_cohort_risk_boundaries_are_closed() -> None:
    lane = _select(risk=AdaptiveGateRiskInput(key_net_pnl_usdc=-0.12))
    cohort = _select(risk=AdaptiveGateRiskInput(cohort_net_pnl_usdc=-0.30))
    assert lane.action_mode is AdaptiveActionMode.SHADOW_BLOCK
    assert cohort.action_mode is AdaptiveActionMode.HALT


def test_enforcement_can_release_rejected_raw_candidate_at_half_risk() -> None:
    raw = _codex(accepted=True)
    adaptive = _select(incumbent_accepted=False)
    applied = apply_adaptive_gate_decision(raw, adaptive, mode="enforcement")
    assert applied.accepted is True
    assert applied.requested_notional_usdc == 25.0
    assert applied.notional_mult == 0.5
    assert applied.entry_offset_bp == raw.entry_offset_bp
    assert applied.metrics["tp_bp"] == raw.metrics["tp_bp"]


def test_candidate_only_and_disabled_path_do_not_mutate_admission() -> None:
    original = _codex(accepted=True)
    adaptive = _select(
        incumbent_accepted=True,
        compatibility=RegimeCompatibility.ADVERSE,
    )
    annotated = apply_adaptive_gate_decision(original, adaptive, mode="candidate-only")
    assert annotated.accepted is True
    assert annotated.notional_mult == original.notional_mult
    assert annotated.metrics["v1461_adaptive_gate"]["action_mode"] == "SHADOW_BLOCK"


def test_policy_hash_covers_threshold_changes() -> None:
    base = AdaptiveGateConfig()
    assert base.policy_hash != replace(base, evidence_max_age_seconds=3600).policy_hash
    assert base.policy_hash != replace(base, probation_notional_usdc=20.0).policy_hash
