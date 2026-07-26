"""Pure settings helpers for the v1.4.59 adaptive-profile runtime.

Candidate profile selection and live enforcement are deliberately separate.
This module only maps settings and evaluates prerequisites; it performs no I/O
and has no order-management capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from src.gridbot.mainnet.v1459_adaptive_profiles import (
    AdaptiveProfileConfig,
    AdaptiveProfileFlags,
)


CANDIDATE_SELECTOR_SETTING: Final = (
    "mainnet_codex_v1459_candidate_selector_enabled"
)
LIVE_ENFORCEMENT_SETTING: Final = (
    "mainnet_codex_v1459_live_enforcement_enabled"
)
ONE_STEP_REPRICE_SETTING: Final = (
    "mainnet_codex_v1459_one_step_reprice_enabled"
)
RUNNER_SETTING: Final = "mainnet_codex_v1459_runner_enabled"
EARLY_FAIL_SETTING: Final = "mainnet_codex_v1459_early_fail_enabled"

ENFORCEMENT_SETTING_REQUIREMENTS: Final[tuple[str, ...]] = (
    CANDIDATE_SELECTOR_SETTING,
    "mainnet_v1459_observation_enabled",
    "mainnet_v1459_observation_persist_session_enabled",
    "mainnet_v1459_observation_record_opportunities_enabled",
    "mainnet_v1459_observation_record_reconciliation_enabled",
)
ONE_STEP_REPRICE_EXECUTOR_NOT_READY: Final = (
    "one_step_reprice_executor_not_ready"
)


@dataclass(frozen=True, slots=True)
class EnforcementReadiness:
    """Fail-closed result for a requested live-enforcement transition."""

    enforcement_requested: bool = False
    ready: bool = False
    missing: tuple[str, ...] = ()


def _enabled(settings: object, name: str) -> bool:
    """Treat only an explicit boolean true as enabled; missing values are off."""

    return getattr(settings, name, False) is True


def build_profile_config(settings: object) -> AdaptiveProfileConfig:
    """Build selector policy flags without consulting live enforcement."""

    return AdaptiveProfileConfig(
        flags=AdaptiveProfileFlags(
            enabled=_enabled(settings, CANDIDATE_SELECTOR_SETTING),
            one_step_reprice_enabled=_enabled(settings, ONE_STEP_REPRICE_SETTING),
            runner_enabled=_enabled(settings, RUNNER_SETTING),
            early_fail_enabled=_enabled(settings, EARLY_FAIL_SETTING),
        )
    )


def evaluate_enforcement_readiness(
    settings: object,
    guard_enabled: bool,
    reconciliation_enabled: bool,
) -> EnforcementReadiness:
    """Return whether requested enforcement has every safety prerequisite."""

    enforcement_requested = _enabled(settings, LIVE_ENFORCEMENT_SETTING)
    if not enforcement_requested:
        return EnforcementReadiness()

    missing = [
        name
        for name in ENFORCEMENT_SETTING_REQUIREMENTS
        if not _enabled(settings, name)
    ]
    if guard_enabled is not True:
        missing.append("guard_enabled")
    if reconciliation_enabled is not True:
        missing.append("reconciliation_enabled")

    return EnforcementReadiness(
        enforcement_requested=True,
        ready=not missing,
        missing=tuple(missing),
    )


__all__ = [
    "CANDIDATE_SELECTOR_SETTING",
    "EARLY_FAIL_SETTING",
    "ENFORCEMENT_SETTING_REQUIREMENTS",
    "EnforcementReadiness",
    "LIVE_ENFORCEMENT_SETTING",
    "ONE_STEP_REPRICE_EXECUTOR_NOT_READY",
    "ONE_STEP_REPRICE_SETTING",
    "RUNNER_SETTING",
    "build_profile_config",
    "evaluate_enforcement_readiness",
]
