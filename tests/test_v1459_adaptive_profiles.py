from dataclasses import FrozenInstanceError, replace

import pytest

from src.gridbot.mainnet.v1459_adaptive_profiles import (
    AdaptiveProfileConfig,
    AdaptiveProfileFlags,
    DecisionReason,
    ENTRY_MENU,
    EXIT_MENU,
    EntryPolicyInput,
    EntryProfile,
    ExitPolicyInput,
    ExitProfile,
    policy_hash,
    select_entry_profile,
    select_exit_profile,
)


def _entry(**overrides: bool | None) -> EntryPolicyInput:
    values: dict[str, bool | None] = {
        "accepted_by_incumbent_gate": True,
        "reprice_window_open": True,
        "signal_still_valid": True,
        "spread_normal": True,
        "ownership_clear": True,
        "cancel_before_replace_required": True,
        "price_still_repriceable": True,
        "reprice_already_used": False,
        "must_expire": False,
    }
    values.update(overrides)
    return EntryPolicyInput(**values)


def _exit(**overrides: bool | None) -> ExitPolicyInput:
    values: dict[str, bool | None] = {
        "position_open": True,
        "hard_sl_present": True,
        "early_window_open": False,
        "minimum_mfe_met": True,
        "adverse_markout": False,
        "direction_still_valid": True,
        "causal_mfe_covers_cost": True,
        "follow_through_valid": True,
        "runner_guards_present": True,
    }
    values.update(overrides)
    return ExitPolicyInput(**values)


def _enabled(**overrides: bool) -> AdaptiveProfileConfig:
    values = {
        "enabled": True,
        "one_step_reprice_enabled": True,
        "runner_enabled": True,
        "early_fail_enabled": True,
    }
    values.update(overrides)
    return AdaptiveProfileConfig(flags=AdaptiveProfileFlags(**values))


def test_closed_menus_are_exact() -> None:
    assert ENTRY_MENU == (
        EntryProfile.INCUMBENT_PASSIVE,
        EntryProfile.ONE_STEP_REPRICE,
        EntryProfile.EXPIRE,
    )
    assert EXIT_MENU == (
        ExitProfile.PROTECT,
        ExitProfile.RUNNER,
        ExitProfile.EARLY_FAIL,
    )
    with pytest.raises(ValueError, match="closed and immutable"):
        AdaptiveProfileConfig(entry_menu=(EntryProfile.INCUMBENT_PASSIVE,))


def test_flags_and_config_are_immutable_and_default_off() -> None:
    config = AdaptiveProfileConfig()
    assert config.flags == AdaptiveProfileFlags()
    assert not any(
        (
            config.flags.enabled,
            config.flags.one_step_reprice_enabled,
            config.flags.runner_enabled,
            config.flags.early_fail_enabled,
        )
    )
    with pytest.raises(FrozenInstanceError):
        config.flags.enabled = True  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        config.policy_name = "changed"  # type: ignore[misc]


def test_default_off_returns_incumbent_entry_even_when_reprice_ready() -> None:
    decision = select_entry_profile(_entry())
    assert decision.profile is EntryProfile.INCUMBENT_PASSIVE
    assert decision.incumbent_eligible is True
    assert decision.adaptive_eligible is False
    assert decision.reason is DecisionReason.FLAGS_OFF_INCUMBENT


def test_default_off_returns_protect_even_when_runner_ready() -> None:
    decision = select_exit_profile(_exit())
    assert decision.profile is ExitProfile.PROTECT
    assert decision.incumbent_eligible is True
    assert decision.adaptive_eligible is False
    assert decision.reason is DecisionReason.FLAGS_OFF_PROTECT


def test_profile_switch_without_master_flag_is_still_fully_off() -> None:
    config = AdaptiveProfileConfig(
        flags=AdaptiveProfileFlags(
            one_step_reprice_enabled=True,
            runner_enabled=True,
            early_fail_enabled=True,
        )
    )
    assert select_entry_profile(_entry(), config).profile is EntryProfile.INCUMBENT_PASSIVE
    assert select_exit_profile(_exit(), config).profile is ExitProfile.PROTECT


def test_entry_never_bypasses_incumbent_gate() -> None:
    decision = select_entry_profile(
        _entry(accepted_by_incumbent_gate=False),
        _enabled(),
    )
    assert decision.profile is EntryProfile.INCUMBENT_PASSIVE
    assert decision.incumbent_eligible is False
    assert decision.adaptive_eligible is False
    assert decision.reason is DecisionReason.INCUMBENT_GATE_NOT_ACCEPTED


def test_one_step_reprice_requires_all_precomputed_safety_facts() -> None:
    decision = select_entry_profile(_entry(), _enabled())
    assert decision.profile is EntryProfile.ONE_STEP_REPRICE
    assert decision.adaptive_eligible is True
    assert decision.reason is DecisionReason.ONE_STEP_REPRICE_ELIGIBLE


@pytest.mark.parametrize(
    "unsafe_fact",
    (
        {"signal_still_valid": False},
        {"spread_normal": False},
        {"ownership_clear": False},
        {"cancel_before_replace_required": False},
        {"price_still_repriceable": False},
        {"reprice_already_used": True},
        {"must_expire": True},
    ),
)
def test_entry_safety_condition_selects_expire(
    unsafe_fact: dict[str, bool],
) -> None:
    decision = select_entry_profile(_entry(**unsafe_fact), _enabled())
    assert decision.profile is EntryProfile.EXPIRE
    assert decision.adaptive_eligible is True
    assert decision.reason is DecisionReason.ENTRY_SAFETY_EXPIRE


def test_entry_incomplete_input_returns_incumbent() -> None:
    decision = select_entry_profile(
        _entry(cancel_before_replace_required=None),
        _enabled(),
    )
    assert decision.profile is EntryProfile.INCUMBENT_PASSIVE
    assert decision.adaptive_eligible is False
    assert decision.reason is DecisionReason.INPUT_INCOMPLETE_INCUMBENT


def test_early_fail_has_safety_priority_over_runner() -> None:
    evidence = _exit(
        early_window_open=True,
        minimum_mfe_met=False,
        adverse_markout=True,
        direction_still_valid=False,
        causal_mfe_covers_cost=True,
        follow_through_valid=True,
        runner_guards_present=True,
    )
    decision = select_exit_profile(evidence, _enabled())
    assert decision.profile is ExitProfile.EARLY_FAIL
    assert decision.adaptive_eligible is True
    assert decision.reason is DecisionReason.EARLY_FAIL_SAFETY_PRIORITY


def test_runner_selected_only_when_early_fail_is_not_eligible() -> None:
    decision = select_exit_profile(_exit(), _enabled())
    assert decision.profile is ExitProfile.RUNNER
    assert decision.adaptive_eligible is True
    assert decision.reason is DecisionReason.RUNNER_ELIGIBLE


def test_runner_cannot_remove_or_bypass_hard_sl() -> None:
    decision = select_exit_profile(
        _exit(hard_sl_present=False),
        _enabled(),
    )
    assert decision.profile is ExitProfile.PROTECT
    assert decision.adaptive_eligible is False
    assert decision.reason is DecisionReason.HARD_SL_NOT_PROVEN


def test_exit_incomplete_input_returns_protect() -> None:
    decision = select_exit_profile(
        _exit(follow_through_valid=None),
        _enabled(),
    )
    assert decision.profile is ExitProfile.PROTECT
    assert decision.adaptive_eligible is False
    assert decision.reason is DecisionReason.INPUT_INCOMPLETE_PROTECT


def test_disabled_profile_does_not_require_its_inputs() -> None:
    runner_only = _enabled(early_fail_enabled=False)
    runner = select_exit_profile(
        _exit(
            early_window_open=None,
            minimum_mfe_met=None,
            adverse_markout=None,
            direction_still_valid=None,
        ),
        runner_only,
    )
    assert runner.profile is ExitProfile.RUNNER

    early_fail_only = _enabled(runner_enabled=False)
    early_fail = select_exit_profile(
        _exit(
            early_window_open=True,
            minimum_mfe_met=False,
            adverse_markout=True,
            causal_mfe_covers_cost=None,
            follow_through_valid=None,
            runner_guards_present=None,
        ),
        early_fail_only,
    )
    assert early_fail.profile is ExitProfile.EARLY_FAIL


def test_policy_hash_and_reason_are_stable() -> None:
    config = _enabled()
    first_hash = policy_hash(config)
    second_hash = policy_hash(replace(config))
    assert first_hash == second_hash
    assert len(first_hash) == 64

    first = select_exit_profile(_exit(), config)
    second = select_exit_profile(_exit(), replace(config))
    assert first.policy_hash == second.policy_hash == first_hash
    assert first.reason.value == second.reason.value == "RUNNER_ELIGIBLE"


def test_policy_hash_changes_when_flags_change() -> None:
    assert policy_hash(AdaptiveProfileConfig()) != policy_hash(_enabled())
