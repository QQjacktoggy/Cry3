from __future__ import annotations

from types import SimpleNamespace

import pytest

from config.settings import Settings
from src.gridbot.mainnet.v1459_observation_composition import (
    CHILD_FLAGS,
    FORBIDDEN_ORDER_CAPABILITIES,
    PARENT_FLAG,
    build_v1459_observation_composition,
    observation_flags_from_settings,
    validate_observation_coordinator_for_manager,
)
from src.gridbot.mainnet.v1459_observation_contract import (
    ObservationContractError,
)
from src.gridbot.mainnet.v1459_observation_coordinator import (
    V1459ObservationCoordinator,
)
from src.gridbot.storage.adaptive_evidence_repository import (
    AdaptiveEvidenceRepository,
)
from src.gridbot.storage.adaptive_result_repository import (
    AdaptiveResultRepository,
)
from src.gridbot.storage.database import Database


def _settings() -> Settings:
    return Settings(
        binance_api_key="test",
        binance_api_secret="test",
        _env_file=None,
    )


def test_settings_compatible_defaults_leave_every_flag_off() -> None:
    flags = observation_flags_from_settings(_settings())
    assert flags.enabled is False
    assert flags.persist_session is False
    assert flags.record_opportunities is False
    assert flags.record_shadow is False
    assert flags.record_reconciliation is False
    assert flags.permits_order_mutation is False


def test_child_flags_cannot_escape_parent_and_values_are_strict_bool() -> None:
    child_name = CHILD_FLAGS["record_shadow"]
    with pytest.raises(ObservationContractError, match="parent"):
        observation_flags_from_settings(
            SimpleNamespace(**{PARENT_FLAG: False, child_name: True})
        )
    with pytest.raises(ObservationContractError, match="must be boolean"):
        observation_flags_from_settings(
            SimpleNamespace(**{PARENT_FLAG: 0})
        )


def test_app_composition_builds_only_observation_dependencies(
    tmp_path,
) -> None:
    db = Database(str(tmp_path / "observation.db"))
    composition = build_v1459_observation_composition(_settings(), db)
    assert isinstance(
        composition.evidence_repository, AdaptiveEvidenceRepository
    )
    assert isinstance(
        composition.result_repository, AdaptiveResultRepository
    )
    assert isinstance(composition.coordinator, V1459ObservationCoordinator)
    assert composition.coordinator.evidence_repo is composition.evidence_repository
    assert composition.coordinator.result_repo is composition.result_repository
    assert composition.coordinator.permits_order_mutation is False
    assert not any(
        callable(getattr(composition.coordinator, name, None))
        for name in FORBIDDEN_ORDER_CAPABILITIES
    )
    assert not any(
        "client" in name or "telegram" in name
        for name in vars(composition.coordinator)
    )


def test_manager_injection_guard_is_optional_and_fail_closed() -> None:
    class Safe:
        permits_order_mutation = False

    class Unsafe:
        permits_order_mutation = True

    class Ambiguous:
        permits_order_mutation = 0

    class Missing:
        pass

    class OrderCapable:
        permits_order_mutation = False

        def create_order(self):
            raise AssertionError

    safe = Safe()
    assert validate_observation_coordinator_for_manager(None) is None
    assert validate_observation_coordinator_for_manager(safe) is safe
    for candidate in (Unsafe(), Ambiguous(), Missing(), OrderCapable()):
        with pytest.raises(ObservationContractError):
            validate_observation_coordinator_for_manager(candidate)
