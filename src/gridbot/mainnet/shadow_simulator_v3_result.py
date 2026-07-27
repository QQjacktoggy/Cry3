"""Immutable result object for authoritative fixed-exit shadow V3."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class ShadowTradeOutcomeV3:
    opportunity_id: str
    variant: str
    fill_model: str
    simulation_version: str
    simulation_scope: str
    side: str
    start_ms: int
    decision_latency_ms: int
    entry_eligible_ms: int
    entry_deadline_ms: int
    outcome_deadline_ms: int
    entry_offset_bp: Decimal
    raw_entry_limit_price: Decimal
    entry_limit_price: Decimal
    quantity: Decimal
    tp_anchor: str
    tp_source_trigger_price: Decimal
    tp_price: Decimal
    sl_anchor: str
    sl_source_trigger_price: Decimal
    sl_price: Decimal
    price_quantization_policy: str
    target_price_policy: str
    fill_status: str
    filled_qty: Decimal
    avg_fill_price: Decimal | None
    first_fill_at_ms: int | None
    first_fill_aggregate_trade_id: int | None
    fill_age_ms: int | None
    exit_at_ms: int | None
    exit_aggregate_trade_id: int | None
    trigger_price: Decimal | None
    exit_price_before_slippage: Decimal | None
    exit_price: Decimal | None
    exit_reason: str | None
    exit_fee_rate: Decimal | None
    mfe_bp: Decimal | None
    mae_bp: Decimal | None
    gross_pnl_usdc: Decimal | None
    commission_usdc: Decimal | None
    funding_usdc: Decimal | None
    net_pnl_usdc: Decimal | None
    wr_eligible: bool
    wr_win: bool | None
    ev_opportunity_eligible: bool
    ev_opportunity_contribution_usdc: Decimal | None
    metric_contract: str
    data_quality: str
    data_quality_reason: str
    coverage_provenance: str
    coverage_proof_end_ms: int
    fee_provenance: str
    funding_provenance: str
    max_hold_policy: str

    @property
    def eligible_for_ranking(self) -> bool:
        """Route ranking is EV/opportunity, so COMPLETE no-fill is eligible."""
        return self.ev_opportunity_eligible

    @property
    def eligible_for_closed_trade_metrics(self) -> bool:
        return self.wr_eligible

    def as_dict(self) -> dict[str, Any]:
        def safe(value: Any) -> Any:
            if isinstance(value, Decimal):
                return format(value, "f")
            if isinstance(value, dict):
                return {key: safe(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [safe(item) for item in value]
            return value
        return safe(asdict(self))


__all__ = ["ShadowTradeOutcomeV3"]
