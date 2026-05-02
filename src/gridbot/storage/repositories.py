"""Data access layer for all database tables.

Each repository handles one table or a closely related group.
"""

import json
import time

from src.gridbot.storage.database import Database


class FuturesTradeRepository:
    """Repository for futures_trades table (from /fapi/v1/userTrades)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert_trade(self, trade: dict) -> None:
        await self._db.execute(
            """INSERT INTO futures_trades
            (trade_id, order_id, symbol, side, price, qty, quote_qty, realized_pnl,
             commission, commission_asset, time_ms, position_side, is_maker, is_grid_trade, fetched_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_id) DO UPDATE SET
                realized_pnl=excluded.realized_pnl,
                commission=excluded.commission,
                fetched_at_ms=excluded.fetched_at_ms""",
            (
                trade["trade_id"], trade["order_id"], trade["symbol"], trade["side"],
                trade["price"], trade["qty"], trade["quote_qty"], trade["realized_pnl"],
                trade["commission"], trade["commission_asset"], trade["time_ms"],
                trade["position_side"], 1 if trade["is_maker"] else 0,
                1 if trade.get("is_grid_trade", True) else 0,
                int(time.time() * 1000),
            ),
        )

    async def upsert_trades(self, trades: list[dict]) -> None:
        for trade in trades:
            await self.upsert_trade(trade)

    async def get_latest_trade_time(self, symbol: str) -> int | None:
        """Get the timestamp of the most recent trade for a symbol."""
        row = await self._db.fetchone(
            "SELECT MAX(time_ms) as max_time FROM futures_trades WHERE symbol = ?",
            (symbol,),
        )
        return row["max_time"] if row and row["max_time"] else None

    async def get_trades(
        self,
        symbol: str,
        since_ms: int = 0,
        grid_only: bool = True,
        limit: int = 500,
    ) -> list[dict]:
        """Get trades for a symbol, optionally filtering for grid-only."""
        if grid_only:
            return await self._db.fetchall(
                """SELECT * FROM futures_trades
                WHERE symbol = ? AND time_ms >= ? AND is_grid_trade = 1
                ORDER BY time_ms ASC LIMIT ?""",
                (symbol, since_ms, limit),
            )
        return await self._db.fetchall(
            """SELECT * FROM futures_trades
            WHERE symbol = ? AND time_ms >= ?
            ORDER BY time_ms ASC LIMIT ?""",
            (symbol, since_ms, limit),
        )

    async def get_trades_in_range(
        self,
        start_ms: int,
        end_ms: int,
        symbol: str | None = None,
    ) -> list[dict]:
        """Get trades within a time range (for grid session symbol inference)."""
        if symbol:
            return await self._db.fetchall(
                "SELECT * FROM futures_trades WHERE time_ms >= ? AND time_ms <= ? AND symbol = ? ORDER BY time_ms",
                (start_ms, end_ms, symbol),
            )
        return await self._db.fetchall(
            "SELECT * FROM futures_trades WHERE time_ms >= ? AND time_ms <= ? ORDER BY time_ms",
            (start_ms, end_ms),
        )


class IncomeRepository:
    """Repository for income_records table (from /fapi/v1/income)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert_record(self, record: dict) -> None:
        await self._db.execute(
            """INSERT INTO income_records
            (tran_id, symbol, income_type, income, asset, time_ms, info, trade_id, is_grid_trade, fetched_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tran_id) DO UPDATE SET
                is_grid_trade=CASE WHEN excluded.is_grid_trade != -1 THEN excluded.is_grid_trade ELSE income_records.is_grid_trade END""",
            (
                record["tran_id"], record["symbol"], record["income_type"],
                record["income"], record["asset"], record["time_ms"],
                record["info"], record["trade_id"],
                record.get("is_grid_trade", -1),
                int(time.time() * 1000),
            ),
        )

    async def upsert_records(self, records: list[dict]) -> None:
        for record in records:
            await self.upsert_record(record)

    async def get_latest_time(
        self,
        income_type: str | None = None,
        symbol: str | None = None,
    ) -> int | None:
        """Get timestamp of the most recent income record.

        Supports per-symbol and per-type filtering to avoid cross-symbol
        watermark skew.
        """
        conditions: list[str] = []
        params: list = []
        if income_type:
            conditions.append("income_type = ?")
            params.append(income_type)
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        row = await self._db.fetchone(
            f"SELECT MAX(time_ms) as max_time FROM income_records{where}",
            tuple(params) if params else (),
        )
        return row["max_time"] if row and row["max_time"] else None

    async def get_records(
        self,
        income_type: str | None = None,
        symbol: str | None = None,
        since_ms: int = 0,
        grid_only: bool = False,
        limit: int = 500,
    ) -> list[dict]:
        """Get income records with optional grid-only filtering.

        grid_only=True excludes manual-trade income (is_grid_trade=0)
        but includes unknown (-1) and grid (1) records.
        """
        conditions = ["time_ms >= ?"]
        params: list = [since_ms]

        if income_type:
            conditions.append("income_type = ?")
            params.append(income_type)
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if grid_only:
            conditions.append("is_grid_trade != 0")

        params.append(limit)
        where = " AND ".join(conditions)
        return await self._db.fetchall(
            f"SELECT * FROM income_records WHERE {where} ORDER BY time_ms ASC LIMIT ?",
            tuple(params),
        )

    async def sum_income(
        self,
        income_type: str,
        symbol: str | None = None,
        since_ms: int = 0,
        grid_only: bool = False,
    ) -> float:
        """Sum income of a given type for a symbol since a timestamp.

        grid_only=True excludes manual-trade income (is_grid_trade=0).
        """
        conditions = ["income_type = ?", "time_ms >= ?"]
        params: list = [income_type, since_ms]

        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if grid_only:
            conditions.append("is_grid_trade != 0")

        where = " AND ".join(conditions)
        row = await self._db.fetchone(
            f"SELECT COALESCE(SUM(income), 0) as total FROM income_records WHERE {where}",
            tuple(params),
        )
        return row["total"] if row else 0.0

    async def get_grid_transfers(self, since_ms: int = 0) -> list[dict]:
        """Get STRATEGY_UMFUTURES_TRANSFER records for grid session tracking."""
        return await self._db.fetchall(
            """SELECT * FROM income_records
            WHERE income_type = 'STRATEGY_UMFUTURES_TRANSFER' AND time_ms >= ?
            ORDER BY time_ms ASC""",
            (since_ms,),
        )


class GridSessionRepository:
    """Repository for grid_sessions table (paired CREATE/CLOSE transfers)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert_session(self, session: dict) -> None:
        await self._db.execute(
            """INSERT INTO grid_sessions
            (symbol, created_at_ms, closed_at_ms, invested_amount, returned_amount,
             net_profit, asset, create_tran_id, close_tran_id, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(create_tran_id) DO UPDATE SET
                symbol=excluded.symbol,
                closed_at_ms=excluded.closed_at_ms,
                returned_amount=excluded.returned_amount,
                net_profit=excluded.net_profit,
                close_tran_id=excluded.close_tran_id,
                is_active=excluded.is_active""",
            (
                session.get("symbol"), session["create_time_ms"],
                session.get("close_time_ms"), session["invested_amount"],
                session.get("returned_amount"), session.get("net_profit"),
                session["asset"], session["create_tran_id"],
                session.get("close_tran_id"),
                1 if session.get("is_active", True) else 0,
            ),
        )

    async def get_active_session(self, symbol: str | None = None) -> dict | None:
        """Get the currently active (running) grid session.

        When symbol is provided, returns only the session for that symbol,
        preventing cross-symbol session contamination.
        """
        if symbol:
            return await self._db.fetchone(
                "SELECT * FROM grid_sessions WHERE is_active = 1 AND symbol = ? ORDER BY created_at_ms DESC LIMIT 1",
                (symbol,),
            )
        return await self._db.fetchone(
            "SELECT * FROM grid_sessions WHERE is_active = 1 ORDER BY created_at_ms DESC LIMIT 1"
        )

    async def get_sessions(self, symbol: str | None = None, limit: int = 20) -> list[dict]:
        if symbol:
            return await self._db.fetchall(
                "SELECT * FROM grid_sessions WHERE symbol = ? ORDER BY created_at_ms DESC LIMIT ?",
                (symbol, limit),
            )
        return await self._db.fetchall(
            "SELECT * FROM grid_sessions ORDER BY created_at_ms DESC LIMIT ?",
            (limit,),
        )

    async def get_total_profit(self, symbol: str | None = None) -> float:
        """Sum of net_profit across all closed sessions."""
        if symbol:
            row = await self._db.fetchone(
                "SELECT COALESCE(SUM(net_profit), 0) as total FROM grid_sessions WHERE is_active = 0 AND symbol = ?",
                (symbol,),
            )
        else:
            row = await self._db.fetchone(
                "SELECT COALESCE(SUM(net_profit), 0) as total FROM grid_sessions WHERE is_active = 0"
            )
        return row["total"] if row else 0.0


class MarketSnapshotRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def save_snapshot(self, snapshot: dict) -> None:
        await self._db.execute(
            """INSERT INTO market_snapshots
            (symbol, snapshot_time_ms, current_price, high_24h, low_24h, volume_24h,
             price_change_pct_24h, funding_rate, next_funding_time_ms, mark_price, klines_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot["symbol"], int(time.time() * 1000),
                snapshot["current_price"], snapshot["high_24h"], snapshot["low_24h"],
                snapshot["volume_24h"], snapshot["price_change_pct_24h"],
                snapshot.get("funding_rate"), snapshot.get("next_funding_time_ms"),
                snapshot.get("mark_price"),
                json.dumps(snapshot.get("klines", [])),
            ),
        )

    async def get_latest(self, symbol: str) -> dict | None:
        return await self._db.fetchone(
            "SELECT * FROM market_snapshots WHERE symbol = ? ORDER BY snapshot_time_ms DESC LIMIT 1",
            (symbol,),
        )

    async def get_history(self, symbol: str, limit: int = 48) -> list[dict]:
        return await self._db.fetchall(
            "SELECT * FROM market_snapshots WHERE symbol = ? ORDER BY snapshot_time_ms DESC LIMIT ?",
            (symbol, limit),
        )


class PerformanceRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def save_snapshot(self, perf: dict) -> None:
        await self._db.execute(
            """INSERT INTO performance_snapshots
            (symbol, snapshot_time_ms, algo_id, strategy_label, realized_pnl, unrealized_pnl,
             funding_cost, fill_rate, price_range_utilization, total_trades,
             leverage, liquidation_price, margin_ratio, apr_estimate, metrics_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                perf["symbol"], int(time.time() * 1000), perf.get("algo_id"),
                perf["strategy_label"], perf["realized_pnl"], perf["unrealized_pnl"],
                perf.get("funding_cost", 0), perf["fill_rate"],
                perf["price_range_utilization"], perf["total_trades"],
                perf.get("leverage"), perf.get("liquidation_price"),
                perf.get("margin_ratio"), perf.get("apr_estimate"),
                json.dumps(perf.get("metrics", {})),
            ),
        )

    async def get_history(self, symbol: str, limit: int = 48) -> list[dict]:
        return await self._db.fetchall(
            "SELECT * FROM performance_snapshots WHERE symbol = ? ORDER BY snapshot_time_ms DESC LIMIT ?",
            (symbol, limit),
        )


class RecommendationRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def save(self, rec: dict) -> int:
        cursor = await self._db.execute(
            """INSERT INTO recommendations
            (created_at_ms, symbol, recommended_strategy, confidence,
             parameter_adjustments_json, market_summary, reasoning,
             risk_warnings_json, trigger, acted_upon)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(time.time() * 1000), rec.get("symbol"),
                rec["recommended_strategy"], rec["confidence"],
                json.dumps(rec.get("parameter_adjustments", [])),
                rec["market_summary"], rec["reasoning"],
                json.dumps(rec.get("risk_warnings", [])),
                rec.get("trigger", "scheduled"), 0,
            ),
        )
        return cursor.lastrowid

    async def mark_acted(self, rec_id: int) -> None:
        await self._db.execute("UPDATE recommendations SET acted_upon = 1 WHERE id = ?", (rec_id,))

    async def get_recent(self, limit: int = 10, symbol: str | None = None) -> list[dict]:
        if symbol:
            return await self._db.fetchall(
                "SELECT * FROM recommendations WHERE symbol = ? ORDER BY created_at_ms DESC LIMIT ?",
                (symbol, limit),
            )
        return await self._db.fetchall(
            "SELECT * FROM recommendations ORDER BY created_at_ms DESC LIMIT ?", (limit,)
        )


class StrategyHistoryRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record_switch(
        self,
        symbol: str,
        previous: str | None,
        new: str,
        reason: str | None = None,
        recommendation_id: int | None = None,
    ) -> None:
        await self._db.execute(
            """INSERT INTO strategy_history
            (symbol, previous_strategy, new_strategy, switch_reason, switched_at_ms, recommendation_id)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (symbol, previous, new, reason, int(time.time() * 1000), recommendation_id),
        )

    async def get_history(self, symbol: str | None = None, limit: int = 20) -> list[dict]:
        if symbol:
            return await self._db.fetchall(
                "SELECT * FROM strategy_history WHERE symbol = ? ORDER BY switched_at_ms DESC LIMIT ?",
                (symbol, limit),
            )
        return await self._db.fetchall(
            "SELECT * FROM strategy_history ORDER BY switched_at_ms DESC LIMIT ?", (limit,)
        )


class AuditLogRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def log(self, event_type: str, actor: str, details: dict) -> None:
        await self._db.execute(
            "INSERT INTO audit_log (event_time_ms, event_type, actor, details_json) VALUES (?, ?, ?, ?)",
            (int(time.time() * 1000), event_type, actor, json.dumps(details, ensure_ascii=False)),
        )


class ConfigRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, key: str) -> str | None:
        row = await self._db.fetchone("SELECT value FROM app_config WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set(self, key: str, value: str) -> None:
        await self._db.execute(
            """INSERT INTO app_config (key, value, updated_at_ms)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at_ms=excluded.updated_at_ms""",
            (key, value, int(time.time() * 1000)),
        )
