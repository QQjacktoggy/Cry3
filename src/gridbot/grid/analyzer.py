"""Grid performance analyzer — computes metrics from FAPI data.

All P&L calculations use actual income records (not estimates).
All inputs are pure data; no API calls are made here.
"""

from src.gridbot.binance.models import AccountInfo, FetchResult, FuturesTrade, IncomeRecord, PositionInfo
from src.gridbot.grid.models import GridMetrics


def compute_metrics(
    result: FetchResult,
    income_records: list[IncomeRecord] | None = None,
    grid_trades: list[FuturesTrade] | None = None,
    session_invested: float | None = None,
    session_start_ms: int | None = None,
    strategy_label: str = "unknown",
) -> GridMetrics:
    """Compute grid metrics from a FetchResult and supplementary income data.

    Args:
        result: FetchResult from the fetcher (trades, market, position, account).
        income_records: Pre-fetched income records for this symbol.
                        If None, uses result.income_records.
        grid_trades: Grid-only trades loaded from the database (is_grid_trade=1).
                     If provided, overrides result.trades for all trade-based
                     statistics (counts, fill rate, grid range, APR).
                     This prevents manual futures trades from polluting metrics.
        session_invested: Investment amount from active grid session.
        session_start_ms: Start time of active grid session.
        strategy_label: Current strategy name for labeling.
    """
    trades = grid_trades if grid_trades is not None else result.trades
    income = income_records if income_records is not None else result.income_records
    position = result.position
    account = result.account
    market = result.market

    # ── P&L from income records (actual, not estimated) ──
    realized_pnl = _sum_income_by_type(income, "REALIZED_PNL")
    commission_total = abs(_sum_income_by_type(income, "COMMISSION"))
    funding_cost = abs(_sum_income_by_type(income, "FUNDING_FEE"))

    unrealized_pnl = position.unrealized_pnl if position else 0.0
    net_pnl = realized_pnl - commission_total - funding_cost + unrealized_pnl

    # ── Trade statistics ──
    buys = [t for t in trades if t.side == "BUY"]
    sells = [t for t in trades if t.side == "SELL"]
    makers = [t for t in trades if t.is_maker]
    takers = [t for t in trades if not t.is_maker]
    total = len(trades)

    maker_ratio = len(makers) / total if total > 0 else 0.0

    # ── Grid range and fill rate ──
    grid_lower, grid_upper = _estimate_grid_range(trades, market.current_price)
    fill_rate = _compute_fill_rate(trades, grid_lower, grid_upper)
    range_util = _compute_price_range_utilization(market.current_price, grid_lower, grid_upper)

    # ── Trade frequency ──
    trades_per_hour, avg_interval = _compute_trade_frequency(trades)

    # ── Running hours ──
    running_hours = _compute_running_hours(trades, session_start_ms)

    # ── APR estimate ──
    apr = _estimate_apr(
        realized_pnl=realized_pnl,
        commission=commission_total,
        funding_cost=funding_cost,
        running_hours=running_hours,
        investment=session_invested,
        position=position,
    )

    # ── Position / risk info ──
    leverage = position.leverage if position else None
    liq_price = position.liquidation_price if position else None
    liq_distance = position.distance_to_liquidation_pct if position else None
    margin_ratio = account.margin_ratio if account else None
    direction = position.position_direction if position else "FLAT"
    pos_size = abs(position.position_amt) if position else None

    # ── Session profit ──
    session_profit = None
    if session_invested is not None:
        session_profit = net_pnl  # net PnL since session start

    return GridMetrics(
        symbol=result.symbol,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        funding_cost=funding_cost,
        commission_total=commission_total,
        net_pnl=net_pnl,
        total_trades=total,
        buy_trades=len(buys),
        sell_trades=len(sells),
        maker_trades=len(makers),
        taker_trades=len(takers),
        maker_ratio=maker_ratio,
        fill_rate=fill_rate,
        price_range_utilization=range_util,
        avg_trade_interval_minutes=avg_interval,
        trades_per_hour=trades_per_hour,
        apr_estimate=apr,
        leverage=leverage,
        liquidation_price=liq_price,
        distance_to_liquidation_pct=liq_distance,
        margin_ratio=margin_ratio,
        position_direction=direction,
        position_size=pos_size,
        grid_lower_price=grid_lower,
        grid_upper_price=grid_upper,
        current_price=market.current_price,
        investment_amount=session_invested,
        grid_session_profit=session_profit,
        running_hours=running_hours,
    )


def _sum_income_by_type(income: list[IncomeRecord], income_type: str) -> float:
    """Sum income values for a specific income type."""
    return sum(r.income for r in income if r.income_type == income_type)


def _estimate_grid_range(trades: list[FuturesTrade], current_price: float) -> tuple[float, float]:
    """Estimate grid range from trade prices."""
    if not trades:
        return current_price * 0.95, current_price * 1.05
    prices = [t.price for t in trades]
    return min(prices), max(prices)


def _compute_fill_rate(trades: list[FuturesTrade], grid_lower: float, grid_upper: float) -> float:
    """Estimate what fraction of grid levels have been triggered."""
    if not trades or grid_lower >= grid_upper:
        return 0.0

    # Count unique price levels that were traded
    unique_prices = set()
    for t in trades:
        unique_prices.add(round(t.price, 2))

    grid_range = grid_upper - grid_lower
    if grid_range <= 0:
        return 0.0

    # Estimate total grid levels from average spacing
    avg_spacing = grid_range / max(len(unique_prices), 1)
    estimated_total_grids = grid_range / avg_spacing if avg_spacing > 0 else len(unique_prices)
    return min(len(unique_prices) / max(estimated_total_grids, 1), 1.0)


def _compute_price_range_utilization(current_price: float, lower: float, upper: float) -> float:
    """How well positioned the current price is within the grid range.

    Returns 0.0 if price is outside the range.
    Returns 0.0-1.0 indicating position within range.
    """
    if lower >= upper:
        return 0.0
    if current_price < lower or current_price > upper:
        return 0.0
    return (current_price - lower) / (upper - lower)


def _compute_trade_frequency(trades: list[FuturesTrade]) -> tuple[float, float | None]:
    """Compute trades per hour and average interval between trades."""
    if len(trades) < 2:
        return 0.0, None

    times = sorted(t.time_ms for t in trades)
    span_hours = (times[-1] - times[0]) / (1000 * 3600)
    if span_hours <= 0:
        return 0.0, None

    trades_per_hour = len(trades) / span_hours
    intervals = [(times[i + 1] - times[i]) / 60000 for i in range(len(times) - 1)]
    avg_interval = sum(intervals) / len(intervals) if intervals else None
    return trades_per_hour, avg_interval


def _compute_running_hours(
    trades: list[FuturesTrade],
    session_start_ms: int | None = None,
) -> float:
    """Compute how long the grid has been running."""
    if session_start_ms and trades:
        latest_trade = max(t.time_ms for t in trades)
        return max((latest_trade - session_start_ms) / (1000 * 3600), 0.001)
    if len(trades) < 2:
        return 0.0
    times = sorted(t.time_ms for t in trades)
    return max((times[-1] - times[0]) / (1000 * 3600), 0.001)


def _estimate_apr(
    realized_pnl: float,
    commission: float,
    funding_cost: float,
    running_hours: float,
    investment: float | None,
    position: PositionInfo | None,
) -> float | None:
    """Estimate annualized percentage return.

    Only computed when running_hours >= 24 to avoid extreme values.
    """
    if running_hours < 24:
        return None

    net = realized_pnl - commission - funding_cost

    # Determine capital base
    capital = investment
    if not capital and position:
        capital = abs(position.position_amt) * position.entry_price
    if not capital or capital <= 0:
        return None

    hourly_return = net / capital
    return hourly_return * 8760 * 100  # annualized percentage
