"""Exact, immutable legacy paid-decision identity for v1.4.69 R1-C1.

The builder is intentionally supplied by the already-resolved legacy selector.
It does not attempt to rediscover or approximate that selector's decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping

from .v1469_adaptive_identity import (
    BreakevenPolicy, DcaPolicy, EarlyFailPolicy, ExecutionProfile,
    MarketStateIdentity, RepricePolicy, RunnerPolicy, TakeProfitLevel,
    TrailPolicy, canonical_sha256,
)
from .v1469_arm_profiles import ArmProfileDefinition

LEGACY_CONTROL = "LEGACY_CONTROL"


@dataclass(frozen=True, slots=True, kw_only=True)
class LegacyExecutionSnapshot:
    """Complete resolved legacy decision; caps/prices remain submit context."""

    market_identity: MarketStateIdentity
    entry_offset_bp: float
    entry_type: str
    entry_ttl_s: int
    maker_mode: str
    take_profits: tuple[TakeProfitLevel, ...]
    sl_bp: float
    max_hold_s: int
    reprice: RepricePolicy
    breakeven: BreakevenPolicy
    trail: TrailPolicy
    runner: RunnerPolicy
    early_fail: EarlyFailPolicy
    dca: DcaPolicy
    lane_notional_cap_usdc: float
    global_notional_cap_usdc: float
    risk_policy_hash: str
    reference_price: float

    def __post_init__(self) -> None:
        if not isinstance(self.market_identity, MarketStateIdentity):
            raise TypeError("market_identity must be MarketStateIdentity")
        for name, kind in (("reprice", RepricePolicy), ("breakeven", BreakevenPolicy),
                           ("trail", TrailPolicy), ("runner", RunnerPolicy),
                           ("early_fail", EarlyFailPolicy), ("dca", DcaPolicy)):
            if not isinstance(getattr(self, name), kind):
                raise TypeError(f"{name} must be {kind.__name__}")
        if not isinstance(self.take_profits, tuple) or not self.take_profits:
            raise TypeError("take_profits must be a non-empty tuple")
        if str(self.entry_type).strip().upper() not in {"LIMIT", "MARKET"}:
            raise ValueError("entry_type must be LIMIT or MARKET")
        for name in ("lane_notional_cap_usdc", "global_notional_cap_usdc", "reference_price"):
            value = getattr(self, name)
            try:
                normalized = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{name} must be finite and positive") from exc
            if isinstance(value, bool) or not isfinite(normalized) or normalized <= 0:
                raise ValueError(f"{name} must be positive")
        if float(self.global_notional_cap_usdc) < float(self.lane_notional_cap_usdc):
            raise ValueError(
                "global_notional_cap_usdc must cover lane_notional_cap_usdc"
            )
        if not str(self.risk_policy_hash or "").strip():
            raise ValueError("risk_policy_hash is required")

    @property
    def execution_profile(self) -> ExecutionProfile:
        # entry_type is represented without loss by the closed maker-mode
        # contract: MARKET requires MARKET, LIMIT requires a limit maker mode.
        entry_type = str(self.entry_type).strip().upper()
        maker = str(self.maker_mode).strip().upper()
        if (entry_type == "MARKET") != (maker == "MARKET"):
            raise ValueError("entry_type and maker_mode conflict")
        return ExecutionProfile(
            profile_id=LEGACY_CONTROL, entry_offset_bp=self.entry_offset_bp,
            entry_ttl_s=self.entry_ttl_s, maker_mode=maker,
            take_profits=self.take_profits, sl_bp=self.sl_bp,
            max_hold_s=self.max_hold_s, reprice=self.reprice,
            breakeven=self.breakeven, trail=self.trail, runner=self.runner,
            early_fail=self.early_fail, dca=self.dca,
        )

    @property
    def profile_definition(self) -> ArmProfileDefinition:
        return ArmProfileDefinition(profile_id=LEGACY_CONTROL,
            allowed_regimes=(self.market_identity.coarse_regime,),
            execution_profile=self.execution_profile)

    def submit_authority_payload(self) -> dict[str, Any]:
        return {"profile_id": LEGACY_CONTROL,
                "execution_profile_hash": self.execution_profile.profile_hash,
                "lane_notional_cap_usdc": float(self.lane_notional_cap_usdc),
                "global_notional_cap_usdc": float(self.global_notional_cap_usdc),
                "risk_policy_hash": str(self.risk_policy_hash),
                "reference_price": float(self.reference_price)}

    def to_payload(self) -> dict[str, Any]:
        """Return the complete canonical, JSON-safe observation snapshot."""
        return {
            "market_identity": self.market_identity.to_payload(),
            "entry_offset_bp": float(self.entry_offset_bp),
            "entry_type": str(self.entry_type).strip().upper(),
            "entry_ttl_s": int(self.entry_ttl_s),
            "maker_mode": str(self.maker_mode).strip().upper(),
            "take_profits": [item.to_payload() for item in self.take_profits],
            "sl_bp": float(self.sl_bp),
            "max_hold_s": int(self.max_hold_s),
            "reprice": self.reprice.to_payload(),
            "breakeven": self.breakeven.to_payload(),
            "trail": self.trail.to_payload(),
            "runner": self.runner.to_payload(),
            "early_fail": self.early_fail.to_payload(),
            "dca": self.dca.to_payload(),
            **self.submit_authority_payload(),
            "submit_authority_hash": self.submit_authority_hash,
        }

    @property
    def submit_authority_hash(self) -> str:
        return canonical_sha256(self.submit_authority_payload())


def build_legacy_execution_snapshot(value: Mapping[str, Any]) -> LegacyExecutionSnapshot:
    """Fail closed on missing/unknown execution-affecting inputs."""
    fields = set(LegacyExecutionSnapshot.__dataclass_fields__)
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown or missing:
        raise ValueError(f"legacy snapshot incomplete: missing={sorted(missing)}, unknown={sorted(unknown)}")
    return LegacyExecutionSnapshot(**dict(value))
