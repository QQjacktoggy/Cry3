"""Observation-only dependency composition for the v1.4.59 foundation.

This module is intentionally independent of App and MainnetOneRunManager so it
can be tested without starting a service. The three small call-site edits
remain a separate, reviewable injection patch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.gridbot.mainnet.v1459_observation_contract import (
    ObservationContractError,
    V1459ObservationFlags,
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


PARENT_FLAG = "mainnet_v1459_observation_enabled"
CHILD_FLAGS = {
    "persist_session": "mainnet_v1459_observation_persist_session_enabled",
    "record_opportunities": (
        "mainnet_v1459_observation_record_opportunities_enabled"
    ),
    "record_shadow": "mainnet_v1459_observation_record_shadow_enabled",
    "record_reconciliation": (
        "mainnet_v1459_observation_record_reconciliation_enabled"
    ),
}
FORBIDDEN_ORDER_CAPABILITIES = (
    "create_order",
    "place_order",
    "submit_order",
    "amend_order",
    "cancel_order",
    "cancel_all_orders",
)


def _strict_bool(settings: Any, name: str) -> bool:
    value = getattr(settings, name, False)
    if not isinstance(value, bool):
        raise ObservationContractError(f"{name} must be boolean")
    return value


def observation_flags_from_settings(settings: Any) -> V1459ObservationFlags:
    """Map Settings-compatible attributes; missing fields remain safely off."""

    return V1459ObservationFlags(
        enabled=_strict_bool(settings, PARENT_FLAG),
        **{
            field: _strict_bool(settings, setting_name)
            for field, setting_name in CHILD_FLAGS.items()
        },
    )


def validate_observation_coordinator_for_manager(
    coordinator: Any | None,
) -> Any | None:
    """Accept only an optional dependency that explicitly cannot mutate orders."""

    if coordinator is None:
        return None
    if getattr(coordinator, "permits_order_mutation", None) is not False:
        raise ObservationContractError(
            "observation coordinator must explicitly declare "
            "permits_order_mutation=False"
        )
    exposed = [
        name for name in FORBIDDEN_ORDER_CAPABILITIES
        if callable(getattr(coordinator, name, None))
    ]
    if exposed:
        raise ObservationContractError(
            "observation coordinator exposes forbidden order capabilities: "
            + ",".join(exposed)
        )
    return coordinator


@dataclass(frozen=True)
class V1459ObservationComposition:
    evidence_repository: AdaptiveEvidenceRepository
    result_repository: AdaptiveResultRepository
    coordinator: V1459ObservationCoordinator

    def __post_init__(self) -> None:
        validate_observation_coordinator_for_manager(self.coordinator)


def build_v1459_observation_composition(
    settings: Any,
    db: Database,
) -> V1459ObservationComposition:
    """Create repositories and coordinator without exchange/Telegram clients."""

    evidence = AdaptiveEvidenceRepository(db)
    results = AdaptiveResultRepository(db)
    coordinator = V1459ObservationCoordinator(
        flags=observation_flags_from_settings(settings),
        evidence_repo=evidence,
        result_repo=results,
    )
    return V1459ObservationComposition(evidence, results, coordinator)


__all__ = [
    "CHILD_FLAGS",
    "FORBIDDEN_ORDER_CAPABILITIES",
    "PARENT_FLAG",
    "V1459ObservationComposition",
    "build_v1459_observation_composition",
    "observation_flags_from_settings",
    "validate_observation_coordinator_for_manager",
]
