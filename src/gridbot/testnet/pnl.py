"""PnL breakdown helpers for live testnet execution."""

from __future__ import annotations

from dataclasses import dataclass

from src.gridbot.binance.models import FuturesTrade, IncomeRecord


@dataclass(frozen=True)
class TestnetPnlBreakdown:
    realized: float
    funding: float
    maker_commission: float
    non_maker_commission: float
    residual_cleanup_commission: float = 0.0

    @property
    def total_commission(self) -> float:
        return self.maker_commission + self.non_maker_commission

    @property
    def maker_fee(self) -> float:
        return abs(self.maker_commission)

    @property
    def non_maker_fee(self) -> float:
        return abs(self.non_maker_commission)

    @property
    def total_fee(self) -> float:
        return abs(self.total_commission)

    @property
    def full_net(self) -> float:
        """Net PnL after all exchange-reported commissions, including maker."""
        return self.realized + self.funding + self.total_commission

    def effective_commission(self, *, ignore_maker_fees: bool) -> float:
        return self.non_maker_commission if ignore_maker_fees else self.total_commission

    def effective_net(self, *, ignore_maker_fees: bool) -> float:
        return self.realized + self.funding + self.effective_commission(ignore_maker_fees=ignore_maker_fees)

    @property
    def residual_cleanup_fee(self) -> float:
        return abs(self.residual_cleanup_commission)


def extract_residual_cleanup_order_ids(
    orders: list[dict],
    *,
    prefix: str = "cry3rc_",
) -> set[int]:
    order_ids: set[int] = set()
    for order in orders:
        client_order_id = str(order.get("clientOrderId") or order.get("newClientOrderId") or "")
        if not client_order_id.startswith(prefix):
            continue
        try:
            order_id = int(order.get("orderId"))
        except (TypeError, ValueError):
            continue
        if order_id > 0:
            order_ids.add(order_id)
    return order_ids


def calculate_testnet_pnl_breakdown(
    records: list[IncomeRecord],
    trades: list[FuturesTrade] | None = None,
    residual_cleanup_order_ids: set[int] | None = None,
) -> TestnetPnlBreakdown:
    """Split realized/funding/commission into maker and non-maker buckets.

    Commission records that cannot be matched to a maker trade are treated as
    non-maker so the full-net number remains conservative.
    """
    realized = sum(item.income for item in records if item.income_type == "REALIZED_PNL")
    funding = sum(item.income for item in records if item.income_type == "FUNDING_FEE")
    maker_trade_ids = {
        str(trade.trade_id)
        for trade in (trades or [])
        if isinstance(trade, FuturesTrade) and trade.is_maker
    }
    trade_by_id = {
        str(trade.trade_id): trade
        for trade in (trades or [])
        if isinstance(trade, FuturesTrade)
    }
    cleanup_order_ids = residual_cleanup_order_ids or set()

    maker_commission = 0.0
    non_maker_commission = 0.0
    residual_cleanup_commission = 0.0
    for record in records:
        if record.income_type != "COMMISSION":
            continue
        trade = trade_by_id.get(str(record.trade_id)) if record.trade_id else None
        if trade is not None and trade.order_id in cleanup_order_ids:
            residual_cleanup_commission += record.income
        if record.trade_id and str(record.trade_id) in maker_trade_ids:
            maker_commission += record.income
        else:
            non_maker_commission += record.income

    return TestnetPnlBreakdown(
        realized=realized,
        funding=funding,
        maker_commission=maker_commission,
        non_maker_commission=non_maker_commission,
        residual_cleanup_commission=residual_cleanup_commission,
    )
