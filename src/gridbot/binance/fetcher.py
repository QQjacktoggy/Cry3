"""Data fetcher — orchestrates periodic data sync from Binance to SQLite.

Handles:
1. Incremental trade sync (only fetch new trades since last known)
2. Income record sync (realized PnL, commissions, funding fees)
3. Grid session tracking (CREATE/CLOSE pairing)
4. Market snapshots
5. Grid trade filtering (exclude manual trades via clientOrderId)
"""

from collections import Counter

from src.gridbot.binance.client import BinanceFuturesClient, is_grid_order
from src.gridbot.binance.models import FetchResult, GridSession, IncomeRecord
from src.gridbot.storage.repositories import (
    AuditLogRepository,
    FuturesTradeRepository,
    GridSessionRepository,
    IncomeRepository,
    MarketSnapshotRepository,
)
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)


class BinanceFetcher:
    """Coordinates data fetching from Binance and persistence to SQLite."""

    def __init__(
        self,
        client: BinanceFuturesClient,
        trade_repo: FuturesTradeRepository,
        income_repo: IncomeRepository,
        session_repo: GridSessionRepository,
        market_repo: MarketSnapshotRepository,
        audit_repo: AuditLogRepository,
    ) -> None:
        self._client = client
        self._trade_repo = trade_repo
        self._income_repo = income_repo
        self._session_repo = session_repo
        self._market_repo = market_repo
        self._audit_repo = audit_repo

    async def fetch_symbol(self, symbol: str) -> FetchResult:
        """Fetch and persist all data for a single trading pair."""
        # Determine incremental sync points
        last_trade_time = await self._trade_repo.get_latest_trade_time(symbol)
        last_income_time = await self._income_repo.get_latest_time()

        trades_since = (last_trade_time + 1) if last_trade_time else None
        income_since = (last_income_time + 1) if last_income_time else None

        # Fetch from Binance
        result = await self._client.fetch_symbol_data(
            symbol=symbol,
            trades_since_ms=trades_since,
            income_since_ms=income_since,
        )

        # Filter and tag grid trades
        grid_trade_order_ids = await self._identify_grid_trades(symbol, result)

        # Persist trades
        for trade in result.trades:
            trade_dict = trade.to_dict()
            trade_dict["is_grid_trade"] = trade.order_id in grid_trade_order_ids
            await self._trade_repo.upsert_trade(trade_dict)

        # Persist income records
        for income in result.income_records:
            await self._income_repo.upsert_record(income.to_dict())

        # Persist market snapshot
        await self._market_repo.save_snapshot(result.market.to_dict())

        # Sync grid sessions (global, not per-symbol)
        await self._sync_grid_sessions()

        # Audit log
        await self._audit_repo.log(
            event_type="FETCH_CYCLE",
            actor="scheduler",
            details={
                "symbol": symbol,
                "new_trades": len(result.trades),
                "new_income_records": len(result.income_records),
                "price": result.market.current_price,
                "funding_rate": result.market.funding_rate,
                "has_position": result.position is not None,
                "position_amt": result.position.position_amt if result.position else 0,
            },
        )

        logger.info(
            "fetch_cycle_complete",
            symbol=symbol,
            new_trades=len(result.trades),
            new_income=len(result.income_records),
            price=result.market.current_price,
        )

        return result

    async def _identify_grid_trades(self, symbol: str, result: FetchResult) -> set[int]:
        """Identify which order IDs belong to grid bot trades.

        Uses allOrders endpoint to check clientOrderId prefix.
        Returns a set of order IDs that are grid trades.
        """
        if not result.trades:
            return set()

        # Get unique order IDs from this batch
        order_ids = {t.order_id for t in result.trades}

        # Fetch order details to check clientOrderId
        try:
            # Use the earliest trade time as start
            earliest = min(t.time_ms for t in result.trades)
            orders = await self._client.get_all_orders(symbol, start_time=earliest - 60000)

            grid_order_ids = set()
            for order in orders:
                if order["orderId"] in order_ids and is_grid_order(order.get("clientOrderId", "")):
                    grid_order_ids.add(order["orderId"])

            logger.debug(
                "grid_trade_filter",
                symbol=symbol,
                total_orders=len(order_ids),
                grid_orders=len(grid_order_ids),
            )
            return grid_order_ids

        except Exception as exc:
            # If we can't determine, assume all are grid trades (safer default)
            logger.warning("grid_filter_failed", symbol=symbol, error=str(exc))
            return order_ids

    async def _sync_grid_sessions(self) -> None:
        """Sync grid sessions from STRATEGY_UMFUTURES_TRANSFER income records."""
        # Fetch grid transfer income records
        transfers = await self._income_repo.get_grid_transfers()
        if not transfers:
            return

        # Separate CREATE and CLOSE events
        creates = [t for t in transfers if t.get("info") == "UM_GRID_CREATE"]
        closes = [t for t in transfers if t.get("info") == "UM_GRID_CLOSE"]

        # Sort by time
        creates.sort(key=lambda x: x["time_ms"])
        closes.sort(key=lambda x: x["time_ms"])

        # Pair them: each CLOSE matches the most recent preceding CREATE
        close_idx = 0
        for create in creates:
            session = GridSession(
                create_time_ms=create["time_ms"],
                close_time_ms=None,
                invested_amount=abs(create["income"]),
                returned_amount=None,
                net_profit=None,
                asset=create["asset"],
                create_tran_id=create["tran_id"],
                close_tran_id=None,
                is_active=True,
            )

            # Find matching CLOSE (first CLOSE after this CREATE)
            while close_idx < len(closes) and closes[close_idx]["time_ms"] <= create["time_ms"]:
                close_idx += 1

            if close_idx < len(closes):
                close = closes[close_idx]
                session.close_time_ms = close["time_ms"]
                session.returned_amount = close["income"]
                session.net_profit = close["income"] - session.invested_amount
                session.close_tran_id = close["tran_id"]
                session.is_active = False
                close_idx += 1

            # Infer symbol from trades in the time window
            session.symbol = await self._infer_session_symbol(session)

            await self._session_repo.upsert_session(session.to_dict())

    async def _infer_session_symbol(self, session: GridSession) -> str | None:
        """Infer which symbol a grid session belongs to by looking at trades in its time window."""
        end_ms = session.close_time_ms or (session.create_time_ms + 86400000)  # +24h if still active
        trades = await self._trade_repo.get_trades_in_range(session.create_time_ms, end_ms)

        if not trades:
            return None

        # Count symbols in the time window
        symbol_counts = Counter(t["symbol"] for t in trades)
        if symbol_counts:
            return symbol_counts.most_common(1)[0][0]
        return None

    async def fetch_global_income(self) -> list[IncomeRecord]:
        """Fetch global income records (STRATEGY_UMFUTURES_TRANSFER, FUNDING_FEE).

        These are not symbol-specific, so fetched separately.
        """
        last_time = await self._income_repo.get_latest_time("STRATEGY_UMFUTURES_TRANSFER")
        since = (last_time + 1) if last_time else None

        records: list[IncomeRecord] = []
        for itype in ["STRATEGY_UMFUTURES_TRANSFER", "FUNDING_FEE"]:
            batch = await self._client.get_income_history(
                income_type=itype,
                start_time=since,
                limit=500,
            )
            records.extend(batch)

        for record in records:
            await self._income_repo.upsert_record(record.to_dict())

        return records

    async def fetch_all_symbols(self, symbols: list[str]) -> dict[str, FetchResult]:
        """Fetch data for all configured trading pairs."""
        # First, fetch global income (grid sessions, funding fees)
        await self.fetch_global_income()

        # Then fetch per-symbol data
        results: dict[str, FetchResult] = {}
        for symbol in symbols:
            try:
                results[symbol] = await self.fetch_symbol(symbol)
            except Exception as exc:
                logger.error("fetch_symbol_failed", symbol=symbol, error=str(exc))
                await self._audit_repo.log(
                    event_type="FETCH_ERROR",
                    actor="scheduler",
                    details={"symbol": symbol, "error": str(exc)},
                )
        return results
