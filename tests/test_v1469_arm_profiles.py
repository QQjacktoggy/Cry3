from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from src.gridbot.mainnet.v1469_adaptive_identity import (
    ExecutionProfile,
    MarketStateIdentity,
)
from src.gridbot.mainnet.v1469_arm_profiles import (
    ARM_PROFILE_MENU,
    PASSIVE_BALANCED,
    RANGE_SCALP,
    RISK_OFF,
    TREND_PARTIAL,
    arm_identity_hash,
    get_arm_profile,
    profiles_for_matched_candidate,
)
from src.gridbot.storage.v1469_arm_observation_repository import arm_identity


def _identity(regime: str, *, side: str = "LONG") -> MarketStateIdentity:
    return MarketStateIdentity(
        environment="MAINNET",
        symbol="BTCUSDT",
        lane_code="W6A",
        effective_side=side,
        strategy="CODEX_V1",
        coarse_regime=regime,
        market_state="mixed",
    )


def test_closed_profile_menu_has_only_the_four_planned_actions() -> None:
    assert tuple(ARM_PROFILE_MENU) == (
        RANGE_SCALP,
        TREND_PARTIAL,
        PASSIVE_BALANCED,
        RISK_OFF,
    )
    for profile_id in (RANGE_SCALP, TREND_PARTIAL, PASSIVE_BALANCED):
        definition = ARM_PROFILE_MENU[profile_id]
        assert isinstance(definition.execution_profile, ExecutionProfile)
        assert definition.execution_profile.profile_id == profile_id
        assert definition.execution_profile_hash
        assert definition.risk_off is False
    assert ARM_PROFILE_MENU[RISK_OFF].execution_profile is None
    assert ARM_PROFILE_MENU[RISK_OFF].risk_off is True


def test_closed_profile_menu_and_definitions_are_immutable() -> None:
    with pytest.raises(TypeError):
        ARM_PROFILE_MENU["NEW_PROFILE"] = (  # type: ignore[index]
            ARM_PROFILE_MENU[RISK_OFF]
        )
    with pytest.raises(FrozenInstanceError):
        ARM_PROFILE_MENU[RANGE_SCALP].profile_id = "CHANGED"  # type: ignore[misc]


@pytest.mark.parametrize("candidate_status", ["SAFE", "NOT_EVALUATED"])
def test_range_candidate_gets_range_passive_and_risk_off(
    candidate_status: str,
) -> None:
    profiles = profiles_for_matched_candidate(
        _identity("RANGE"), candidate_status
    )

    assert tuple(profile.profile_id for profile in profiles) == (
        RANGE_SCALP,
        PASSIVE_BALANCED,
        RISK_OFF,
    )


@pytest.mark.parametrize("regime", ["TREND_UP", "TREND_DOWN"])
def test_trend_candidate_gets_trend_passive_and_risk_off(regime: str) -> None:
    profiles = profiles_for_matched_candidate(_identity(regime), "SAFE")

    assert tuple(profile.profile_id for profile in profiles) == (
        TREND_PARTIAL,
        PASSIVE_BALANCED,
        RISK_OFF,
    )


@pytest.mark.parametrize("regime", ["SHOCK", "UNCERTAIN"])
def test_invalid_or_shock_regime_gets_only_risk_off(regime: str) -> None:
    profiles = profiles_for_matched_candidate(
        _identity(regime), "NOT_EVALUATED"
    )

    assert tuple(profile.profile_id for profile in profiles) == (RISK_OFF,)


def test_candidate_status_fails_closed_outside_shadow_contract() -> None:
    with pytest.raises(ValueError, match="SAFE or NOT_EVALUATED"):
        profiles_for_matched_candidate(_identity("RANGE"), "BLOCKED")


def test_arm_identity_hash_is_deterministic_and_profile_specific() -> None:
    identity = _identity("RANGE")
    range_profile = get_arm_profile(RANGE_SCALP)
    passive_profile = get_arm_profile(PASSIVE_BALANCED)

    assert arm_identity_hash(identity, range_profile) == arm_identity_hash(
        identity, range_profile
    )
    assert arm_identity_hash(identity, range_profile) != arm_identity_hash(
        identity, passive_profile
    )
    assert len(arm_identity_hash(identity, range_profile)) == 71


def test_arm_identity_does_not_fragment_on_detail_state() -> None:
    first = _identity("RANGE")
    second = MarketStateIdentity(
        environment=first.environment,
        symbol=first.symbol,
        lane_code=first.lane_code,
        effective_side=first.effective_side,
        strategy=first.strategy,
        coarse_regime=first.coarse_regime,
        market_state="RANGE_VOLATILE_DETAIL",
    )
    profile = get_arm_profile(RANGE_SCALP)

    assert first.identity_hash != second.identity_hash
    assert arm_identity_hash(first, profile) == arm_identity_hash(
        second, profile
    )
    assert arm_identity_hash(first, profile) == arm_identity(
        {
            "lane_code": first.lane_code,
            "effective_side": first.effective_side,
            "strategy": first.strategy,
            "coarse_regime": first.coarse_regime,
            "execution_profile_id": profile.profile_id,
            "execution_profile_schema": (
                profile.execution_profile.to_payload()["schema"]
            ),
            "execution_profile_hash": profile.execution_profile_hash,
        }
    )


def test_profile_cannot_be_hashed_for_an_illegal_regime() -> None:
    with pytest.raises(ValueError, match="not legal"):
        arm_identity_hash(
            _identity("RANGE"),
            get_arm_profile(TREND_PARTIAL),
        )


def test_profile_geometry_matches_frozen_initial_menu() -> None:
    range_profile = get_arm_profile(RANGE_SCALP).execution_profile
    trend_profile = get_arm_profile(TREND_PARTIAL).execution_profile
    passive_profile = get_arm_profile(PASSIVE_BALANCED).execution_profile
    assert range_profile is not None
    assert trend_profile is not None
    assert passive_profile is not None

    assert (
        range_profile.entry_offset_bp,
        range_profile.entry_ttl_s,
        range_profile.sl_bp,
        range_profile.max_hold_s,
    ) == (1.0, 90, 8.0, 360)
    assert [
        (level.level_id, level.target_bp, level.fraction)
        for level in range_profile.take_profits
    ] == [("FULL", 8.0, 1.0)]
    assert [
        (level.level_id, level.target_bp, level.fraction)
        for level in trend_profile.take_profits
    ] == [("TP1", 6.0, 0.7), ("FULL", 16.0, 0.3)]
    assert (
        passive_profile.entry_offset_bp,
        passive_profile.entry_ttl_s,
        passive_profile.sl_bp,
    ) == (2.0, 120, 12.0)
