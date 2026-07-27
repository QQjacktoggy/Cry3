import pytest

from src.gridbot.strategy.live_next.contracts import ContractError
from src.gridbot.strategy.live_next.execution_policy import (
    EntryExecutionMode,
    ExecutionPolicy,
)


def test_three_v5_execution_modes_are_explicit_and_deterministic() -> None:
    maker = ExecutionPolicy(
        profile_id="maker_near_0bp",
        mode="MAKER",
        base_latency_ms=100,
        entry_offset_bps=0.0,
        entry_ttl_ms=1_500,
    )
    taker = ExecutionPolicy(
        profile_id="taker_confirm_100ms",
        mode="TAKER_CONFIRM",
        base_latency_ms=100,
        entry_offset_bps=0.0,
        entry_ttl_ms=1_000,
    )
    hybrid = ExecutionPolicy(
        profile_id="hybrid_maker0_500ms",
        mode="HYBRID",
        base_latency_ms=100,
        entry_offset_bps=0.0,
        entry_ttl_ms=2_000,
        maker_phase_ms=500,
    )

    assert maker.mode is EntryExecutionMode.MAKER and not maker.can_take
    assert taker.mode is EntryExecutionMode.TAKER_CONFIRM and taker.can_take
    assert hybrid.mode is EntryExecutionMode.HYBRID and hybrid.can_take
    assert hybrid.to_replay_profile().entry_ttl_ms == 2_000


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "TAKER_CONFIRM", "entry_offset_bps": 1.0},
        {"mode": "HYBRID", "maker_phase_ms": 0},
        {"mode": "HYBRID", "maker_phase_ms": 2_000},
        {"mode": "MAKER", "maker_phase_ms": 100},
        {"mode": "MAKER", "max_reprices": 1},
    ],
)
def test_execution_policy_fails_closed_on_ambiguous_semantics(changes) -> None:
    values = {
        "profile_id": "candidate",
        "mode": "MAKER",
        "base_latency_ms": 100,
        "entry_offset_bps": 0.0,
        "entry_ttl_ms": 2_000,
        "maker_phase_ms": 0,
    }
    values.update(changes)

    with pytest.raises(ContractError):
        ExecutionPolicy(**values)
