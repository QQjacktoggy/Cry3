from __future__ import annotations

from types import SimpleNamespace

from src.gridbot.mainnet.v1459_adaptive_profiles import AdaptiveProfileFlags
from src.gridbot.mainnet.v1459_profile_runtime import (
    EnforcementReadiness,
    build_profile_config,
    evaluate_enforcement_readiness,
)


def _enforcement_settings(**overrides: bool) -> SimpleNamespace:
    values = {
        "mainnet_codex_v1459_candidate_selector_enabled": True,
        "mainnet_codex_v1459_live_enforcement_enabled": True,
        "mainnet_codex_v1459_one_step_reprice_enabled": False,
        "mainnet_v1459_observation_enabled": True,
        "mainnet_v1459_observation_persist_session_enabled": True,
        "mainnet_v1459_observation_record_opportunities_enabled": True,
        "mainnet_v1459_observation_record_reconciliation_enabled": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_missing_settings_leave_profile_and_enforcement_off() -> None:
    settings = SimpleNamespace()

    assert build_profile_config(settings).flags == AdaptiveProfileFlags()
    assert evaluate_enforcement_readiness(settings, True, True) == (
        EnforcementReadiness()
    )


def test_profile_config_uses_candidate_selector_not_live_enforcement() -> None:
    candidate = SimpleNamespace(
        mainnet_codex_v1459_candidate_selector_enabled=True,
        mainnet_codex_v1459_live_enforcement_enabled=False,
        mainnet_codex_v1459_runner_enabled=True,
        mainnet_codex_v1459_early_fail_enabled=True,
        mainnet_codex_v1459_one_step_reprice_enabled=True,
    )
    enforcement_only = SimpleNamespace(
        mainnet_codex_v1459_candidate_selector_enabled=False,
        mainnet_codex_v1459_live_enforcement_enabled=True,
    )

    assert build_profile_config(candidate).flags == AdaptiveProfileFlags(
        enabled=True,
        one_step_reprice_enabled=True,
        runner_enabled=True,
        early_fail_enabled=True,
    )
    assert build_profile_config(enforcement_only).flags == AdaptiveProfileFlags()


def test_disabled_enforcement_does_not_evaluate_prerequisites() -> None:
    settings = SimpleNamespace(
        mainnet_codex_v1459_candidate_selector_enabled=True,
        mainnet_codex_v1459_live_enforcement_enabled=False,
        mainnet_codex_v1459_one_step_reprice_enabled=True,
    )

    assert evaluate_enforcement_readiness(settings, False, False) == (
        EnforcementReadiness()
    )


def test_enforcement_lists_every_missing_safety_prerequisite() -> None:
    settings = SimpleNamespace(
        mainnet_codex_v1459_live_enforcement_enabled=True,
    )

    readiness = evaluate_enforcement_readiness(settings, False, False)

    assert readiness.enforcement_requested is True
    assert readiness.ready is False
    assert readiness.missing == (
        "mainnet_codex_v1459_candidate_selector_enabled",
        "mainnet_v1459_observation_enabled",
        "mainnet_v1459_observation_persist_session_enabled",
        "mainnet_v1459_observation_record_opportunities_enabled",
        "mainnet_v1459_observation_record_reconciliation_enabled",
        "guard_enabled",
        "reconciliation_enabled",
    )


def test_enforcement_is_ready_when_all_prerequisites_are_enabled() -> None:
    readiness = evaluate_enforcement_readiness(
        _enforcement_settings(),
        guard_enabled=True,
        reconciliation_enabled=True,
    )

    assert readiness == EnforcementReadiness(
        enforcement_requested=True,
        ready=True,
        missing=(),
    )


def test_one_step_reprice_does_not_block_ready_enforcement() -> None:
    readiness = evaluate_enforcement_readiness(
        _enforcement_settings(
            mainnet_codex_v1459_one_step_reprice_enabled=True,
        ),
        guard_enabled=True,
        reconciliation_enabled=True,
    )

    assert readiness.enforcement_requested is True
    assert readiness.ready is True
    assert readiness.missing == ()
