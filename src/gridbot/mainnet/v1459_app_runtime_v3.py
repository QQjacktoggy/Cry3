"""Final flag-validated wrapper for restart-safe v1.4.59 App bootstrap."""

from __future__ import annotations

from typing import Any

from src.gridbot.mainnet.v1459_app_runtime_v2 import (
    V1459AppRuntimeV2,
    build_v1459_app_runtime_v2,
)
from src.gridbot.mainnet.v1459_flag_policy import (
    validate_v1459_flag_dependencies,
)
from src.gridbot.mainnet.v1459_observation_composition import (
    observation_flags_from_settings,
)
from src.gridbot.storage.database import Database


async def build_v1459_app_runtime_v3(
    *,
    settings: Any,
    db: Database,
    read_only_identity_client: Any,
    code_version: str,
) -> V1459AppRuntimeV2:
    validate_v1459_flag_dependencies(observation_flags_from_settings(settings))
    return await build_v1459_app_runtime_v2(
        settings=settings,
        db=db,
        read_only_identity_client=read_only_identity_client,
        code_version=code_version,
    )


__all__ = ["build_v1459_app_runtime_v3"]
