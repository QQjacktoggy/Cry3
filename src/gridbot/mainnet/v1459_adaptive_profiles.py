"""Pure, finite adaptive-profile policy for Codex v1.4.59.

This module deliberately contains no prices, quantities, time windows, or trading
thresholds.  Callers must evaluate the frozen v1.4.58 evidence and pass only the
resulting facts.  The selectors return policy intent; they never place, cancel, or
replace orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from typing import Final


class EntryProfile(str, Enum):
    """Closed entry-policy menu."""

    INCUMBENT_PASSIVE = "INCUMBENT_PASSIVE"
    ONE_STEP_REPRICE = "ONE_STEP_REPRICE"
    EXPIRE = "EXPIRE"


class ExitProfile(str, Enum):
    """Closed exit-policy menu."""

    PROTECT = "PROTECT"
    RUNNER = "RUNNER"
    EARLY_FAIL = "EARLY_FAIL"


class DecisionReason(str, Enum):
    """Stable, machine-readable reasons emitted by the selectors."""

    FLAGS_OFF_INCUMBENT = "FLAGS_OFF_INCUMBENT"
    FLAGS_OFF_PROTECT = "FLAGS_OFF_PROTECT"
    INPUT_INCOMPLETE_INCUMBENT = "INPUT_INCOMPLETE_INCUMBENT"
    INPUT_INCOMPLETE_PROTECT = "INPUT_INCOMPLETE_PROTECT"
    INCUMBENT_GATE_NOT_ACCEPTED = "INCUMBENT_GATE_NOT_ACCEPTED"
    NO_OPEN_POSITION = "NO_OPEN_POSITION"
    ENTRY_SAFETY_EXPIRE = "ENTRY_SAFETY_EXPIRE"
    ONE_STEP_REPRICE_ELIGIBLE = "ONE_STEP_REPRICE_ELIGIBLE"
    INCUMBENT_ENTRY_CONTINUES = "INCUMBENT_ENTRY_CONTINUES"
    HARD_SL_NOT_PROVEN = "HARD_SL_NOT_PROVEN"
    EARLY_FAIL_SAFETY_PRIORITY = "EARLY_FAIL_SAFETY_PRIORITY"
    RUNNER_ELIGIBLE = "RUNNER_ELIGIBLE"
    PROTECT_POSITION = "PROTECT_POSITION"


ENTRY_MENU: Final[tuple[EntryProfile, ...]] = (
    EntryProfile.INCUMBENT_PASSIVE,
    EntryProfile.ONE_STEP_REPRICE,
    EntryProfile.EXPIRE,
)
EXIT_MENU: Final[tuple[ExitProfile, ...]] = (
    ExitProfile.PROTECT,
    ExitProfile.RUNNER,
    ExitProfile.EARLY_FAIL,
)


@dataclass(frozen=True, slots=True)
class AdaptiveProfileFlags:
    """Default-off switches for the finite v1.4.59 policy."""

    enabled: bool = False
    one_step_reprice_enabled: bool = False
    runner_enabled: bool = False
    early_fail_enabled: bool = False


@dataclass(frozen=True, slots=True)
class AdaptiveProfileConfig:
    """Immutable policy identity; it intentionally has no tunable numbers."""

    flags: AdaptiveProfileFlags = field(default_factory=AdaptiveProfileFlags)
    policy_name: str = "codex_v1.4.59-finite-adaptive-profiles"
    entry_menu: tuple[EntryProfile, ...] = ENTRY_MENU
    exit_menu: tuple[ExitProfile, ...] = EXIT_MENU

    def __post_init__(self) -> None:
        if self.entry_menu != ENTRY_MENU or self.exit_menu != EXIT_MENU:
            raise ValueError("v1.4.59 adaptive profile menus are closed and immutable")


@dataclass(frozen=True, slots=True)
class EntryPolicyInput:
    """Pre-evaluated entry facts; ``None`` means evidence is incomplete."""

    accepted_by_incumbent_gate: bool | None
    reprice_window_open: bool | None
    signal_still_valid: bool | None
    spread_normal: bool | None
    ownership_clear: bool | None
    cancel_before_replace_required: bool | None
    price_still_repriceable: bool | None
    reprice_already_used: bool | None
    must_expire: bool | None


@dataclass(frozen=True, slots=True)
class ExitPolicyInput:
    """Pre-evaluated exit facts; ``None`` means evidence is incomplete."""

    position_open: bool | None
    hard_sl_present: bool | None
    early_window_open: bool | None
    minimum_mfe_met: bool | None
    adverse_markout: bool | None
    direction_still_valid: bool | None
    causal_mfe_covers_cost: bool | None
    follow_through_valid: bool | None
    runner_guards_present: bool | None


@dataclass(frozen=True, slots=True)
class EntryPolicyDecision:
    """Pure entry intent; no order parameters or side are carried."""

    profile: EntryProfile
    adaptive_eligible: bool
    incumbent_eligible: bool
    reason: DecisionReason
    policy_hash: str


@dataclass(frozen=True, slots=True)
class ExitPolicyDecision:
    """Pure exit intent; execution remains the incumbent runtime's job."""

    profile: ExitProfile
    adaptive_eligible: bool
    incumbent_eligible: bool
    reason: DecisionReason
    policy_hash: str


def policy_hash(config: AdaptiveProfileConfig) -> str:
    """Return a stable SHA-256 identity for the complete finite policy config."""

    canonical = {
        "entry_menu": [profile.value for profile in config.entry_menu],
        "exit_menu": [profile.value for profile in config.exit_menu],
        "flags": {
            "early_fail_enabled": config.flags.early_fail_enabled,
            "enabled": config.flags.enabled,
            "one_step_reprice_enabled": config.flags.one_step_reprice_enabled,
            "runner_enabled": config.flags.runner_enabled,
        },
        "policy_name": config.policy_name,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def select_entry_profile(
    evidence: EntryPolicyInput,
    config: AdaptiveProfileConfig = AdaptiveProfileConfig(),
) -> EntryPolicyDecision:
    """Select an entry intent without changing the incumbent admission decision."""

    identity = policy_hash(config)
    incumbent_eligible = evidence.accepted_by_incumbent_gate is True

    if not config.flags.enabled or not config.flags.one_step_reprice_enabled:
        return EntryPolicyDecision(
            profile=EntryProfile.INCUMBENT_PASSIVE,
            adaptive_eligible=False,
            incumbent_eligible=incumbent_eligible,
            reason=DecisionReason.FLAGS_OFF_INCUMBENT,
            policy_hash=identity,
        )

    facts = (
        evidence.accepted_by_incumbent_gate,
        evidence.reprice_window_open,
        evidence.signal_still_valid,
        evidence.spread_normal,
        evidence.ownership_clear,
        evidence.cancel_before_replace_required,
        evidence.price_still_repriceable,
        evidence.reprice_already_used,
        evidence.must_expire,
    )
    if any(fact is None for fact in facts):
        return EntryPolicyDecision(
            profile=EntryProfile.INCUMBENT_PASSIVE,
            adaptive_eligible=False,
            incumbent_eligible=incumbent_eligible,
            reason=DecisionReason.INPUT_INCOMPLETE_INCUMBENT,
            policy_hash=identity,
        )

    if not evidence.accepted_by_incumbent_gate:
        return EntryPolicyDecision(
            profile=EntryProfile.INCUMBENT_PASSIVE,
            adaptive_eligible=False,
            incumbent_eligible=False,
            reason=DecisionReason.INCUMBENT_GATE_NOT_ACCEPTED,
            policy_hash=identity,
        )

    expire_required = (
        evidence.must_expire
        or not evidence.signal_still_valid
        or not evidence.spread_normal
        or not evidence.ownership_clear
        or not evidence.cancel_before_replace_required
        or not evidence.price_still_repriceable
        or evidence.reprice_already_used
    )
    if expire_required:
        return EntryPolicyDecision(
            profile=EntryProfile.EXPIRE,
            adaptive_eligible=True,
            incumbent_eligible=True,
            reason=DecisionReason.ENTRY_SAFETY_EXPIRE,
            policy_hash=identity,
        )

    if evidence.reprice_window_open:
        return EntryPolicyDecision(
            profile=EntryProfile.ONE_STEP_REPRICE,
            adaptive_eligible=True,
            incumbent_eligible=True,
            reason=DecisionReason.ONE_STEP_REPRICE_ELIGIBLE,
            policy_hash=identity,
        )

    return EntryPolicyDecision(
        profile=EntryProfile.INCUMBENT_PASSIVE,
        adaptive_eligible=False,
        incumbent_eligible=True,
        reason=DecisionReason.INCUMBENT_ENTRY_CONTINUES,
        policy_hash=identity,
    )


def select_exit_profile(
    evidence: ExitPolicyInput,
    config: AdaptiveProfileConfig = AdaptiveProfileConfig(),
) -> ExitPolicyDecision:
    """Select an exit intent, with EARLY_FAIL taking safety priority over RUNNER."""

    identity = policy_hash(config)
    incumbent_eligible = evidence.position_open is True
    exit_adaptive_enabled = (
        config.flags.enabled
        and (config.flags.runner_enabled or config.flags.early_fail_enabled)
    )
    if not exit_adaptive_enabled:
        return ExitPolicyDecision(
            profile=ExitProfile.PROTECT,
            adaptive_eligible=False,
            incumbent_eligible=incumbent_eligible,
            reason=DecisionReason.FLAGS_OFF_PROTECT,
            policy_hash=identity,
        )

    common_facts = (evidence.position_open, evidence.hard_sl_present)
    early_fail_facts = (
        evidence.early_window_open,
        evidence.minimum_mfe_met,
        evidence.adverse_markout,
        evidence.direction_still_valid,
    )
    runner_facts = (
        evidence.causal_mfe_covers_cost,
        evidence.follow_through_valid,
        evidence.runner_guards_present,
    )
    required_facts = common_facts
    if config.flags.early_fail_enabled:
        required_facts += early_fail_facts
    if config.flags.runner_enabled:
        required_facts += runner_facts
    if any(fact is None for fact in required_facts):
        return ExitPolicyDecision(
            profile=ExitProfile.PROTECT,
            adaptive_eligible=False,
            incumbent_eligible=incumbent_eligible,
            reason=DecisionReason.INPUT_INCOMPLETE_PROTECT,
            policy_hash=identity,
        )

    if not evidence.position_open:
        return ExitPolicyDecision(
            profile=ExitProfile.PROTECT,
            adaptive_eligible=False,
            incumbent_eligible=False,
            reason=DecisionReason.NO_OPEN_POSITION,
            policy_hash=identity,
        )

    if not evidence.hard_sl_present:
        return ExitPolicyDecision(
            profile=ExitProfile.PROTECT,
            adaptive_eligible=False,
            incumbent_eligible=True,
            reason=DecisionReason.HARD_SL_NOT_PROVEN,
            policy_hash=identity,
        )

    early_fail_eligible = (
        config.flags.early_fail_enabled
        and evidence.early_window_open is True
        and evidence.minimum_mfe_met is False
        and (
            evidence.adverse_markout is True
            or evidence.direction_still_valid is False
        )
    )
    if early_fail_eligible:
        return ExitPolicyDecision(
            profile=ExitProfile.EARLY_FAIL,
            adaptive_eligible=True,
            incumbent_eligible=True,
            reason=DecisionReason.EARLY_FAIL_SAFETY_PRIORITY,
            policy_hash=identity,
        )

    runner_eligible = (
        config.flags.runner_enabled
        and evidence.causal_mfe_covers_cost is True
        and evidence.follow_through_valid is True
        and evidence.runner_guards_present is True
    )
    if runner_eligible:
        return ExitPolicyDecision(
            profile=ExitProfile.RUNNER,
            adaptive_eligible=True,
            incumbent_eligible=True,
            reason=DecisionReason.RUNNER_ELIGIBLE,
            policy_hash=identity,
        )

    return ExitPolicyDecision(
        profile=ExitProfile.PROTECT,
        adaptive_eligible=False,
        incumbent_eligible=True,
        reason=DecisionReason.PROTECT_POSITION,
        policy_hash=identity,
    )


__all__ = [
    "AdaptiveProfileConfig",
    "AdaptiveProfileFlags",
    "DecisionReason",
    "ENTRY_MENU",
    "EXIT_MENU",
    "EntryPolicyDecision",
    "EntryPolicyInput",
    "EntryProfile",
    "ExitPolicyDecision",
    "ExitPolicyInput",
    "ExitProfile",
    "policy_hash",
    "select_entry_profile",
    "select_exit_profile",
]
