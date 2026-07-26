"""Immutable Live Next entry execution policies shared by replay and shadow."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite

from .contracts import ContractError
from .replay import ExecutionProfile


class EntryExecutionMode(str, Enum):
    MAKER = "MAKER"
    TAKER_CONFIRM = "TAKER_CONFIRM"
    HYBRID = "HYBRID"


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    profile_id: str
    mode: EntryExecutionMode | str
    base_latency_ms: int
    entry_offset_bps: float
    entry_ttl_ms: int
    maker_phase_ms: int = 0
    maker_fill_model: str = "TRADE_THROUGH"
    max_reprices: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ContractError("execution policy profile_id is required")
        object.__setattr__(self, "mode", EntryExecutionMode(self.mode))
        for name in ("base_latency_ms", "entry_ttl_ms", "maker_phase_ms", "max_reprices"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractError(f"{name} must be a non-negative integer")
        if self.entry_ttl_ms <= 0:
            raise ContractError("entry_ttl_ms must be positive")
        if (
            isinstance(self.entry_offset_bps, bool)
            or not isinstance(self.entry_offset_bps, (int, float))
            or not isfinite(float(self.entry_offset_bps))
            or float(self.entry_offset_bps) < 0.0
        ):
            raise ContractError("entry_offset_bps must be non-negative and finite")
        if self.maker_fill_model not in {"TOUCH", "TRADE_THROUGH"}:
            raise ContractError("unknown maker_fill_model")
        if self.max_reprices != 0:
            raise ContractError("v5 research execution policies prohibit repricing")
        if self.mode is EntryExecutionMode.MAKER:
            if self.maker_phase_ms != 0:
                raise ContractError("pure maker policy cannot define maker_phase_ms")
        elif self.mode is EntryExecutionMode.TAKER_CONFIRM:
            if self.entry_offset_bps != 0.0 or self.maker_phase_ms != 0:
                raise ContractError("taker confirmation requires zero offset and no maker phase")
        elif not 0 < self.maker_phase_ms < self.entry_ttl_ms:
            raise ContractError("hybrid maker phase must be inside entry TTL")

    @property
    def can_take(self) -> bool:
        return self.mode in {
            EntryExecutionMode.TAKER_CONFIRM,
            EntryExecutionMode.HYBRID,
        }

    def to_replay_profile(self) -> ExecutionProfile:
        return ExecutionProfile(
            profile_id=self.profile_id,
            entry_offset_bps=float(self.entry_offset_bps),
            entry_ttl_ms=self.entry_ttl_ms,
        )


__all__ = ["EntryExecutionMode", "ExecutionPolicy"]
