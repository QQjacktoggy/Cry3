from __future__ import annotations

import pytest

from src.gridbot.mainnet.v1465_w6a_profile_selector import (
    W6A_PROFILES,
    W6A_SELECTOR_LEASE_TTL_SECONDS,
    W6AEvidenceError,
    W6AProfileEvidence,
    classify_w6a_market_state,
    evaluate_w6a_profile,
    parse_w6a_profile_evidence,
    select_w6a_winner,
)


NOW = 10_000_000


def _row(
    profile_id: str,
    opportunity_id: str,
    age_minutes: float,
    outcome: str,
    net_pnl_bp: float,
    **updates: object,
) -> dict[str, object]:
    observed = NOW - int(age_minutes * 60_000)
    row: dict[str, object] = {
        "profile_id": profile_id,
        "opportunity_id": opportunity_id,
        "observed_at_ms": observed,
        "terminal_at_ms": max(observed, NOW - 1),
        "terminal_outcome": outcome,
        "net_pnl_bp": net_pnl_bp,
        "data_complete": True,
        "ambiguous": False,
        "diagnostic_only": False,
    }
    row.update(updates)
    return row


def _positive_profile(
    profile_id: str,
    *,
    tp_value: float = 4.0,
    loss_value: float = -8.0,
    prefix: str = "",
) -> list[dict[str, object]]:
    rows = [
        _row(profile_id, f"{prefix}p{index}", age, "TP", tp_value)
        for index, age in enumerate((2, 5, 10, 20, 25, 40, 50, 60), start=1)
    ]
    # The one old loss makes guard evidence evaluable without tripping 15m.
    rows.append(_row(profile_id, f"{prefix}loss", 70, "SL", loss_value))
    return rows


def test_fixed_profiles_are_full_exit_and_disable_expansion_controls() -> None:
    assert {
        key: (
            profile.entry_offset_bp,
            profile.tp_bp,
            profile.sl_bp,
            profile.entry_ttl_seconds,
        )
        for key, profile in W6A_PROFILES.items()
    } == {
        "W6A_BASE": (0.0, 6.0, 20.0, 180),
        "W6A_TIGHT": (0.0, 6.0, 10.0, 90),
        "W6A_PASSIVE": (2.0, 8.0, 12.0, 120),
    }
    assert all(profile.full_exit for profile in W6A_PROFILES.values())
    assert all(profile.partial_exit_pct == 1.0 for profile in W6A_PROFILES.values())
    assert all(not profile.dca_enabled for profile in W6A_PROFILES.values())
    assert all(not profile.runner_enabled for profile in W6A_PROFILES.values())
    assert all(
        not profile.one_step_reprice_enabled for profile in W6A_PROFILES.values()
    )
    assert W6A_SELECTOR_LEASE_TTL_SECONDS == 600


def test_state_classifier_covers_reclaim_mixed_falling_and_missing() -> None:
    reclaim = classify_w6a_market_state(
        {
            "setup_age_sec": 180,
            "d30": -20,
            "vwap_dist_bp": -20,
            "pullback_from_recent_high_bp": 20,
            "price_above_or_reclaimed_vwap": 1,
        }
    )
    assert reclaim.state == "reclaim"
    assert reclaim.missing == ()

    mixed = classify_w6a_market_state(
        {
            "setup_age_sec": 301,
            "d30": -20,
            "vwap_dist_bp": -20,
            "pullback_from_recent_high_bp": 20,
            "price_above_or_reclaimed_vwap": 1,
        }
    )
    assert mixed.state == "mixed"

    falling = classify_w6a_market_state(
        {
            "setup_age_sec": 350,
            "d30": -35,
            "vwap_dist_bp": -50,
            "pullback_from_recent_high_bp": 30,
            "price_above_or_reclaimed_vwap": 0,
        }
    )
    assert falling.state == "falling_trap"
    assert falling.risk_score == 5

    missing = classify_w6a_market_state({"d30": -100})
    assert missing.state == "mixed"
    assert "setup_age_sec" in missing.missing
    assert "price_above_or_reclaimed_vwap" in missing.missing


def test_evidence_parser_strictly_excludes_non_authoritative_and_future() -> None:
    valid = _row("W6A_BASE", "valid", 1, "TP", 6)
    assert W6AProfileEvidence.from_mapping(valid, as_of_ms=NOW).evaluable

    cases = (
        ({**valid, "diagnostic_only": True}, "diagnostic"),
        ({**valid, "data_complete": False}, "incomplete"),
        ({**valid, "ambiguous": True}, "ambiguous"),
        ({**valid, "terminal_outcome": "ambiguous_both"}, "ambiguous"),
        ({**valid, "terminal_at_ms": NOW + 1}, "future_terminal"),
        ({**valid, "observed_at_ms": NOW + 1, "terminal_at_ms": NOW + 1}, "future_observation"),
    )
    for row, reason in cases:
        parsed = parse_w6a_profile_evidence(row, as_of_ms=NOW)
        assert parsed.evidence is None
        assert parsed.reason == reason
        with pytest.raises(W6AEvidenceError, match=reason):
            W6AProfileEvidence.from_mapping(row, as_of_ms=NOW)


def test_window_cutoffs_are_inclusive_and_observed_at_assigns_window() -> None:
    rows = _positive_profile("W6A_BASE")
    rows.extend(
        [
            _row("W6A_BASE", "at15", 15, "TP", 4),
            _row("W6A_BASE", "before15", 15 + 1 / 60_000, "SL", -20),
        ]
    )
    # A terminal timestamp inside 15m does not move an old observation into it.
    rows[-1]["terminal_at_ms"] = NOW - 1
    summary = evaluate_w6a_profile(rows, "W6A_BASE", NOW)
    assert summary.metrics["safety_evaluable"] == 4
    assert summary.metrics["safety_sl"] == 0
    assert summary.metrics["authority_evaluable"] == 7


def test_insufficient_evidence_blocks_and_positive_episode_is_eligible() -> None:
    insufficient = evaluate_w6a_profile(
        [_row("W6A_BASE", "one", 5, "TP", 4)], "W6A_BASE", NOW
    )
    assert not insufficient.eligible
    assert "authority_insufficient_evaluable" in insufficient.blockers
    assert "guard_insufficient_evaluable" in insufficient.blockers

    positive = evaluate_w6a_profile(
        _positive_profile("W6A_BASE"), "W6A_BASE", NOW
    )
    assert positive.eligible
    assert positive.metrics["authority_evaluable"] == 5


def test_15m_latest_sl_veto_and_two_sl_veto() -> None:
    rows = _positive_profile("W6A_BASE")
    rows.append(_row("W6A_BASE", "latest-loss", 1, "SL", -8))
    latest = evaluate_w6a_profile(rows, "W6A_BASE", NOW)
    assert "safety_latest_sl" in latest.blockers

    rows = _positive_profile("W6A_BASE")
    rows.extend(
        [
            _row("W6A_BASE", "loss-a", 14, "SL", -8),
            _row("W6A_BASE", "loss-b", 12, "SL", -8),
            _row("W6A_BASE", "latest-win", 1, "TP", 4),
        ]
    )
    two_sl = evaluate_w6a_profile(rows, "W6A_BASE", NOW)
    assert "safety_sl_limit" in two_sl.blockers
    assert "safety_latest_sl" not in two_sl.blockers


def test_90m_guard_checks_ev_and_sl_ratio() -> None:
    rows = [
        _row("W6A_BASE", f"g{index}", age, outcome, value)
        for index, (age, outcome, value) in enumerate(
            [
                (2, "TP", 10),
                (5, "TP", 10),
                (10, "TP", 10),
                (20, "TP", 10),
                (35, "SL", -30),
                (45, "SL", -30),
                (55, "SL", -30),
                (65, "SL", -30),
            ]
        )
    ]
    summary = evaluate_w6a_profile(rows, "W6A_BASE", NOW)
    assert "guard_ev_not_above_threshold" in summary.blockers
    assert "guard_sl_ratio_above_limit" in summary.blockers


def test_fee_net_ev_is_per_opportunity_and_includes_no_fill_zero() -> None:
    rows = _positive_profile("W6A_BASE", tp_value=4)
    rows.extend(
        [
            _row("W6A_BASE", "nf-a", 3, "NO_FILL", 0),
            _row("W6A_BASE", "nf-b", 4, "NO_FILL", 0),
        ]
    )

    summary = evaluate_w6a_profile(rows, "W6A_BASE", NOW)

    authority_rows = [
        row
        for row in rows
        if float(row["observed_at_ms"]) >= NOW - 30 * 60_000
    ]
    expected = sum(float(row["net_pnl_bp"]) for row in authority_rows) / len(
        authority_rows
    )
    assert summary.metrics["authority_ev_bp"] == pytest.approx(expected)


def test_selector_hysteresis_requires_ev_delta_and_three_paired_wins() -> None:
    base = _positive_profile("W6A_BASE", tp_value=4)
    tight = _positive_profile("W6A_TIGHT", tp_value=5.5)
    passive = _positive_profile("W6A_PASSIVE", tp_value=4.5)
    retained = select_w6a_winner(
        base + tight + passive, NOW, current_winner_profile_id="W6A_BASE"
    )
    assert retained.winner_profile_id == "W6A_BASE"
    assert retained.reason == "incumbent_retained_hysteresis"

    # Reuse opportunity ids so the higher-EV challenger also earns paired wins.
    tight = _positive_profile("W6A_TIGHT", tp_value=8)
    switched = select_w6a_winner(
        base + tight + passive, NOW, current_winner_profile_id="W6A_BASE"
    )
    assert switched.winner_profile_id == "W6A_TIGHT"
    assert switched.changed
    assert switched.metrics["selected_paired_wins"] >= 3
    assert switched.lease_ttl_seconds == 600


def test_ineligible_incumbent_falls_back_to_highest_ev_with_deterministic_tie() -> None:
    tight = _positive_profile("W6A_TIGHT", tp_value=6)
    passive = _positive_profile("W6A_PASSIVE", tp_value=6)
    decision = select_w6a_winner(
        tight + passive, NOW, current_winner_profile_id="W6A_BASE"
    )
    assert decision.winner_profile_id == "W6A_TIGHT"
    assert decision.reason == "highest_ev_incumbent_ineligible"
