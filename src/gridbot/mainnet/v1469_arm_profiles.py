"""Closed v1.4.69 adaptive-arm profile menu.

The menu is deliberately static and shadow-only.  It contains no exchange,
storage, runtime, or feature-flag dependency.  A later integration phase may
persist/evaluate these definitions, but it must not mutate them in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from src.gridbot.mainnet.v1469_adaptive_identity import (
    EXECUTION_PROFILE_SCHEMA,
    BreakevenPolicy,
    DcaPolicy,
    EarlyFailPolicy,
    ExecutionProfile,
    MarketStateIdentity,
    RepricePolicy,
    RunnerPolicy,
    TakeProfitLevel,
    TrailPolicy,
    canonical_sha256,
)


ARM_PROFILE_MENU_SCHEMA = "v1469.arm-profile-menu.1"
ARM_IDENTITY_SCHEMA = "v1469.arm-identity.1"

RANGE_SCALP = "RANGE_SCALP"
TREND_PARTIAL = "TREND_PARTIAL"
PASSIVE_BALANCED = "PASSIVE_BALANCED"
RISK_OFF = "RISK_OFF"

_MATCHED_CANDIDATE_STATUSES = frozenset({"SAFE", "NOT_EVALUATED"})
_TREND_REGIMES = frozenset({"TREND_UP", "TREND_DOWN"})


def _disabled_reprice() -> RepricePolicy:
    return RepricePolicy()


def _disabled_breakeven() -> BreakevenPolicy:
    return BreakevenPolicy()


def _disabled_trail() -> TrailPolicy:
    return TrailPolicy()


def _disabled_runner() -> RunnerPolicy:
    return RunnerPolicy()


def _disabled_early_fail() -> EarlyFailPolicy:
    return EarlyFailPolicy()


def _disabled_dca() -> DcaPolicy:
    return DcaPolicy()


@dataclass(frozen=True, slots=True, kw_only=True)
class ArmProfileDefinition:
    profile_id: str
    allowed_regimes: tuple[str, ...]
    execution_profile: ExecutionProfile | None
    risk_off: bool = False

    def __post_init__(self) -> None:
        profile_id = str(self.profile_id or "").strip().upper()
        regimes = tuple(
            str(regime or "").strip().upper() for regime in self.allowed_regimes
        )
        if profile_id not in {
            RANGE_SCALP,
            TREND_PARTIAL,
            PASSIVE_BALANCED,
            RISK_OFF,
        }:
            raise ValueError("profile_id is not in the closed v1.4.69 menu")
        if not regimes or any(
            regime
            not in {"TREND_UP", "TREND_DOWN", "RANGE", "SHOCK", "UNCERTAIN"}
            for regime in regimes
        ):
            raise ValueError("allowed_regimes contains an unsupported regime")
        if len(set(regimes)) != len(regimes):
            raise ValueError("allowed_regimes must not contain duplicates")
        if not isinstance(self.risk_off, bool):
            raise ValueError("risk_off must be a boolean")
        if self.risk_off:
            if profile_id != RISK_OFF or self.execution_profile is not None:
                raise ValueError("RISK_OFF must not carry execution geometry")
        elif (
            profile_id == RISK_OFF
            or not isinstance(self.execution_profile, ExecutionProfile)
        ):
            raise ValueError("tradable profiles require ExecutionProfile geometry")
        elif self.execution_profile.profile_id != profile_id:
            raise ValueError("profile_id must match execution_profile.profile_id")
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "allowed_regimes", regimes)

    @property
    def execution_profile_hash(self) -> str | None:
        if self.execution_profile is None:
            return None
        return self.execution_profile.profile_hash

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": ARM_PROFILE_MENU_SCHEMA,
            "profile_id": self.profile_id,
            "allowed_regimes": list(self.allowed_regimes),
            "risk_off": self.risk_off,
            "execution_profile_schema": (
                EXECUTION_PROFILE_SCHEMA
                if self.execution_profile is not None
                else None
            ),
            "execution_profile_hash": self.execution_profile_hash,
        }


_RANGE_SCALP_PROFILE = ExecutionProfile(
    profile_id=RANGE_SCALP,
    entry_offset_bp=1.0,
    entry_ttl_s=90,
    maker_mode="POST_ONLY",
    reprice=_disabled_reprice(),
    take_profits=(
        TakeProfitLevel(level_id="FULL", target_bp=8.0, fraction=1.0),
    ),
    sl_bp=8.0,
    breakeven=_disabled_breakeven(),
    max_hold_s=360,
    trail=_disabled_trail(),
    runner=_disabled_runner(),
    early_fail=_disabled_early_fail(),
    dca=_disabled_dca(),
)

_TREND_PARTIAL_PROFILE = ExecutionProfile(
    profile_id=TREND_PARTIAL,
    entry_offset_bp=2.0,
    entry_ttl_s=60,
    maker_mode="POST_ONLY",
    reprice=_disabled_reprice(),
    take_profits=(
        TakeProfitLevel(level_id="TP1", target_bp=6.0, fraction=0.70),
        TakeProfitLevel(level_id="FULL", target_bp=16.0, fraction=0.30),
    ),
    sl_bp=10.0,
    breakeven=_disabled_breakeven(),
    max_hold_s=720,
    trail=_disabled_trail(),
    runner=_disabled_runner(),
    early_fail=_disabled_early_fail(),
    dca=_disabled_dca(),
)

_PASSIVE_BALANCED_PROFILE = ExecutionProfile(
    profile_id=PASSIVE_BALANCED,
    entry_offset_bp=2.0,
    entry_ttl_s=120,
    maker_mode="POST_ONLY",
    reprice=_disabled_reprice(),
    take_profits=(
        TakeProfitLevel(level_id="FULL", target_bp=8.0, fraction=1.0),
    ),
    sl_bp=12.0,
    breakeven=_disabled_breakeven(),
    max_hold_s=360,
    trail=_disabled_trail(),
    runner=_disabled_runner(),
    early_fail=_disabled_early_fail(),
    dca=_disabled_dca(),
)


ARM_PROFILE_MENU: Mapping[str, ArmProfileDefinition] = MappingProxyType(
    {
        RANGE_SCALP: ArmProfileDefinition(
            profile_id=RANGE_SCALP,
            allowed_regimes=("RANGE",),
            execution_profile=_RANGE_SCALP_PROFILE,
        ),
        TREND_PARTIAL: ArmProfileDefinition(
            profile_id=TREND_PARTIAL,
            allowed_regimes=("TREND_UP", "TREND_DOWN"),
            execution_profile=_TREND_PARTIAL_PROFILE,
        ),
        PASSIVE_BALANCED: ArmProfileDefinition(
            profile_id=PASSIVE_BALANCED,
            allowed_regimes=("TREND_UP", "TREND_DOWN", "RANGE"),
            execution_profile=_PASSIVE_BALANCED_PROFILE,
        ),
        RISK_OFF: ArmProfileDefinition(
            profile_id=RISK_OFF,
            allowed_regimes=(
                "TREND_UP",
                "TREND_DOWN",
                "RANGE",
                "SHOCK",
                "UNCERTAIN",
            ),
            execution_profile=None,
            risk_off=True,
        ),
    }
)


def get_arm_profile(profile_id: str) -> ArmProfileDefinition:
    normalized = str(profile_id or "").strip().upper()
    try:
        return ARM_PROFILE_MENU[normalized]
    except KeyError as exc:
        raise ValueError("profile_id is not in the closed v1.4.69 menu") from exc


def profiles_for_matched_candidate(
    market_identity: MarketStateIdentity,
    candidate_status: str,
) -> tuple[ArmProfileDefinition, ...]:
    """Return legal paired arms for one already-matched candidate.

    Only SAFE and NOT_EVALUATED candidates belong to this shadow comparison
    contract.  SHOCK and UNCERTAIN states fail closed to RISK_OFF.
    """

    if not isinstance(market_identity, MarketStateIdentity):
        raise TypeError("market_identity must be MarketStateIdentity")
    status = str(candidate_status or "").strip().upper()
    if status not in _MATCHED_CANDIDATE_STATUSES:
        raise ValueError("candidate_status must be SAFE or NOT_EVALUATED")

    regime = market_identity.coarse_regime
    if regime == "RANGE":
        profile_ids = (RANGE_SCALP, PASSIVE_BALANCED, RISK_OFF)
    elif regime in _TREND_REGIMES:
        profile_ids = (TREND_PARTIAL, PASSIVE_BALANCED, RISK_OFF)
    else:
        profile_ids = (RISK_OFF,)
    return tuple(ARM_PROFILE_MENU[profile_id] for profile_id in profile_ids)


def arm_identity_hash(
    market_identity: MarketStateIdentity,
    profile: ArmProfileDefinition,
) -> str:
    if not isinstance(market_identity, MarketStateIdentity):
        raise TypeError("market_identity must be MarketStateIdentity")
    if not isinstance(profile, ArmProfileDefinition):
        raise TypeError("profile must be ArmProfileDefinition")
    if market_identity.coarse_regime not in profile.allowed_regimes:
        raise ValueError("profile is not legal for the supplied coarse regime")
    # This canonical payload intentionally matches storage.arm_identity().
    # Raw/detail market-state identity is retained separately on terminal
    # evidence, but must not fragment one lane/side/coarse-regime/profile arm.
    return "v1469a_" + canonical_sha256(
        {
            "lane_code": market_identity.lane_code,
            "effective_side": market_identity.effective_side,
            "strategy": market_identity.strategy,
            "coarse_regime": market_identity.coarse_regime,
            "execution_profile_id": profile.profile_id,
            "execution_profile_schema": (
                EXECUTION_PROFILE_SCHEMA
                if profile.execution_profile is not None
                else None
            ),
            "execution_profile_hash": profile.execution_profile_hash,
        }
    )


__all__ = [
    "ARM_IDENTITY_SCHEMA",
    "ARM_PROFILE_MENU",
    "ARM_PROFILE_MENU_SCHEMA",
    "ArmProfileDefinition",
    "PASSIVE_BALANCED",
    "RANGE_SCALP",
    "RISK_OFF",
    "TREND_PARTIAL",
    "arm_identity_hash",
    "get_arm_profile",
    "profiles_for_matched_candidate",
]
