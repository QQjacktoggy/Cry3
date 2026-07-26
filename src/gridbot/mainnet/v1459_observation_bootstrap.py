"""Composition root for the v1.4.59 observation runtime.

The caller must supply an observed identity.  For a brand-new session the
expected identity is the same snapshot.  A restart must instead pass the
identity restored from the durable adaptive session as ``expected_identity``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.gridbot.mainnet.runtime_identity import RuntimeIdentity
from src.gridbot.mainnet.v1459_observation_composition import (
    V1459ObservationComposition,
    build_v1459_observation_composition,
)
from src.gridbot.mainnet.v1459_observation_runtime import (
    V1459ObservationRuntime,
    V1459RuntimeContext,
)
from src.gridbot.storage.database import Database


@dataclass(frozen=True)
class V1459ObservationBootstrap:
    composition: V1459ObservationComposition
    runtime: V1459ObservationRuntime

    @property
    def permits_order_mutation(self) -> bool:
        return False


def build_v1459_observation_bootstrap(
    *,
    settings: Any,
    db: Database,
    observed_identity: RuntimeIdentity,
    code_version: str,
    expected_identity: RuntimeIdentity | None = None,
) -> V1459ObservationBootstrap:
    """Build persistence and runtime without any exchange/order dependency."""

    composition = build_v1459_observation_composition(settings, db)
    context = V1459RuntimeContext(
        expected_identity=expected_identity or observed_identity,
        observed_identity=observed_identity,
        code_version=code_version,
    )
    runtime = V1459ObservationRuntime(
        coordinator=composition.coordinator,
        context=context,
    )
    return V1459ObservationBootstrap(composition=composition, runtime=runtime)


__all__ = [
    "V1459ObservationBootstrap",
    "build_v1459_observation_bootstrap",
]
