from __future__ import annotations

import pytest

from src.gridbot.mainnet.v1459_flag_policy import (
    validate_v1459_flag_dependencies,
)
from src.gridbot.mainnet.v1459_observation_contract import (
    ObservationContractError,
    V1459ObservationFlags,
)


def test_disabled_and_session_only_flags_are_valid() -> None:
    assert not validate_v1459_flag_dependencies(V1459ObservationFlags()).enabled
    flags = V1459ObservationFlags(enabled=True, persist_session=True)
    assert validate_v1459_flag_dependencies(flags) is flags


@pytest.mark.parametrize(
    "child",
    ["record_opportunities", "record_shadow", "record_reconciliation"],
)
def test_every_evidence_child_requires_durable_session(child: str) -> None:
    kwargs = {"enabled": True, child: True}
    with pytest.raises(ObservationContractError, match="persist_session"):
        validate_v1459_flag_dependencies(V1459ObservationFlags(**kwargs))


def test_shadow_requires_opportunity_parent() -> None:
    with pytest.raises(ObservationContractError, match="record_opportunities"):
        validate_v1459_flag_dependencies(
            V1459ObservationFlags(
                enabled=True,
                persist_session=True,
                record_shadow=True,
            )
        )


def test_full_evidence_graph_is_valid() -> None:
    flags = V1459ObservationFlags(
        enabled=True,
        persist_session=True,
        record_opportunities=True,
        record_shadow=True,
        record_reconciliation=True,
    )
    assert validate_v1459_flag_dependencies(flags) is flags

