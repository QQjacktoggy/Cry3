"""Cross-flag dependencies for the v1.4.59 durable evidence graph."""

from __future__ import annotations

from src.gridbot.mainnet.v1459_observation_contract import (
    ObservationContractError,
    V1459ObservationFlags,
)


def validate_v1459_flag_dependencies(
    flags: V1459ObservationFlags,
) -> V1459ObservationFlags:
    if not isinstance(flags, V1459ObservationFlags):
        raise ObservationContractError("formal observation flags are required")
    children = (
        flags.record_opportunities,
        flags.record_shadow,
        flags.record_reconciliation,
    )
    if any(children) and not flags.persist_session:
        raise ObservationContractError(
            "durable evidence children require persist_session"
        )
    if flags.record_shadow and not flags.record_opportunities:
        raise ObservationContractError(
            "record_shadow requires record_opportunities"
        )
    return flags


__all__ = ["validate_v1459_flag_dependencies"]
