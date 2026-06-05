"""Match Telegram manual signals with mainnet user trades.

The matcher is read-only against Binance. It turns a pushed signal into a
structured audit trail that can later be used by strategy analysis or AI review.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from html import escape
from typing import Any

from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.binance.models import FuturesTrade, PositionInfo
from src.gridbot.storage.database import Database
from src.gridbot.storage.repositories import AuditLogRepository
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)


def _mainnet_settings(settings: Settings) -> Settings | None:
    if not settings.manual_mainnet_api_key or not settings.manual_mainnet_api_secret:
        return None
    return settings.model_copy(
        update={
            "binance_api_key": settings.manual_mainnet_api_key,
            "binance_api_secret": settings.manual_mainnet_api_secret,
            "binance_testnet": False,
        }
    )


def _trade_dict(trade: FuturesTrade) -> dict[str, Any]:
    return {
        "trade_id": trade.trade_id,
        "order_id": trade.order_id,
        "symbol": trade.symbol,
        "side": trade.side,
        "price": trade.price,
        "qty": abs(trade.qty),
        "quote_qty": abs(trade.quote_qty) if trade.quote_qty else abs(trade.price * trade.qty),
        "realized_pnl": trade.realized_pnl,
        "commission": trade.commission,
        "commission_asset": trade.commission_asset,
        "time_ms": trade.time_ms,
        "position_side": trade.position_side,
        "is_maker": trade.is_maker,
    }


def _position_dict(position: PositionInfo | None) -> dict[str, Any]:
    if position is None:
        return {"qty": 0.0, "side": "", "entry_price": 0.0, "mark_price": 0.0, "unrealized_pnl": 0.0}
    qty = float(getattr(position, "qty", getattr(position, "position_amt", 0.0)) or 0.0)
    return {
        "qty": qty,
        "side": getattr(position, "side", ""),
        "entry_price": float(getattr(position, "entry_price", 0.0) or 0.0),
        "mark_price": float(getattr(position, "mark_price", 0.0) or 0.0),
        "unrealized_pnl": float(getattr(position, "unrealized_pnl", 0.0) or 0.0),
    }


def _weighted_avg_price(trades: list[FuturesTrade]) -> float | None:
    qty = sum(abs(trade.qty) for trade in trades)
    if qty <= 0:
        return None
    quote = sum(abs(trade.quote_qty) if trade.quote_qty else abs(trade.price * trade.qty) for trade in trades)
    return quote / qty


def _slippage_bps(direction: str, planned_entry: float | None, avg_entry: float | None) -> float | None:
    if not planned_entry or planned_entry <= 0 or avg_entry is None:
        return None
    if direction == "short":
        return (planned_entry - avg_entry) / planned_entry * 10_000
    return (avg_entry - planned_entry) / planned_entry * 10_000


def _safe_json(details_json: str | None) -> dict[str, Any]:
    try:
        details = json.loads(details_json or "{}")
    except json.JSONDecodeError:
        return {}
    return details if isinstance(details, dict) else {}


class ManualSignalMatcher:
    """Periodically links signal notifications to actual mainnet trades."""

    def __init__(self, settings: Settings, db: Database, telegram_app=None) -> None:
        self._settings = settings
        self._db = db
        self._audit_repo = AuditLogRepository(db)
        self._telegram_app = telegram_app
        self._running = False

    async def run_match_cycle(self) -> None:
        if not self._settings.manual_signal_auto_match_enabled:
            return
        if self._running:
            logger.info("manual_signal_auto_match_skipped", reason="already_running")
            return
        mainnet_settings = _mainnet_settings(self._settings)
        if mainnet_settings is None:
            logger.info("manual_signal_auto_match_skipped", reason="mainnet_api_not_configured")
            return

        self._running = True
        try:
            await self._run_match_cycle(mainnet_settings)
        except Exception as exc:
            logger.error("manual_signal_auto_match_failed", error=str(exc))
        finally:
            self._running = False

    async def _run_match_cycle(self, mainnet_settings: Settings) -> None:
        signals = await self._recent_manual_signals()
        if not signals:
            return
        previous = await self._previous_match_events()
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        pending = []
        for signal in signals:
            execution_id = str(signal.get("execution_id") or "")
            if not execution_id:
                continue
            prev = previous.get(execution_id)
            if prev and prev.get("status") == "expired":
                continue
            pending.append(signal)
        if not pending:
            return

        client = BinanceFuturesClient(mainnet_settings)
        await client.connect()
        try:
            trades_by_symbol: dict[str, list[FuturesTrade]] = {}
            positions_by_symbol: dict[str, PositionInfo | None] = {}
            for symbol in sorted({str(item.get("symbol") or "") for item in pending if item.get("symbol")}):
                earliest = min(self._window_start_ms(item) for item in pending if item.get("symbol") == symbol)
                trades_by_symbol[symbol] = await client.get_user_trades(symbol=symbol, start_time=earliest, limit=1000)
                positions_by_symbol[symbol] = await client.get_position(symbol)

            next_signal_sent_at = self._next_signal_sent_times(signals)
            for signal in pending:
                summary = self._build_signal_match_summary(
                    signal=signal,
                    trades=trades_by_symbol.get(str(signal.get("symbol") or ""), []),
                    position=positions_by_symbol.get(str(signal.get("symbol") or "")),
                    now_ms=now_ms,
                    next_signal_sent_at_ms=next_signal_sent_at.get(str(signal.get("execution_id") or "")),
                )
                await self._record_if_changed(summary, previous.get(summary["execution_id"]))
        finally:
            await client.close()

    async def _recent_manual_signals(self) -> list[dict[str, Any]]:
        cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - max(
            1, int(self._settings.manual_signal_auto_match_lookback_hours)
        ) * 3_600_000
        rows = await self._db.fetchall(
            """SELECT id, event_time_ms, details_json
            FROM audit_log
            WHERE event_type = ? AND event_time_ms >= ?
            ORDER BY event_time_ms ASC
            LIMIT 200""",
            ("manual_signal_sent", cutoff_ms),
        )
        signals = []
        for row in rows:
            details = _safe_json(row.get("details_json"))
            if not details:
                continue
            details["audit_id"] = row["id"]
            details["sent_event_time_ms"] = int(row["event_time_ms"] or 0)
            details["sent_at_ms"] = int(details.get("notified_at_ms") or row["event_time_ms"] or 0)
            signals.append(details)
        return signals

    async def _previous_match_events(self) -> dict[str, dict[str, Any]]:
        cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - max(
            1, int(self._settings.manual_signal_auto_match_lookback_hours)
        ) * 3_600_000
        rows = await self._db.fetchall(
            """SELECT event_time_ms, event_type, details_json
            FROM audit_log
            WHERE event_type IN (?, ?) AND event_time_ms >= ?
            ORDER BY event_time_ms ASC""",
            ("manual_signal_auto_matched", "manual_signal_auto_match_expired", cutoff_ms),
        )
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            details = _safe_json(row.get("details_json"))
            execution_id = str(details.get("execution_id") or "")
            if execution_id:
                details["event_type"] = row["event_type"]
                details["matched_event_time_ms"] = int(row["event_time_ms"] or 0)
                latest[execution_id] = details
        return latest

    def _window_start_ms(self, signal: dict[str, Any]) -> int:
        pre_ms = max(0, int(self._settings.manual_signal_match_pre_window_seconds)) * 1000
        return max(0, int(signal.get("sent_at_ms") or 0) - pre_ms)

    def _window_end_ms(self, signal: dict[str, Any]) -> int:
        return int(signal.get("sent_at_ms") or 0) + max(1, int(self._settings.manual_signal_match_window_minutes)) * 60_000

    def _bounded_window_end_ms(self, signal: dict[str, Any], next_signal_sent_at_ms: int | None, now_ms: int) -> int:
        end_ms = self._window_end_ms(signal)
        if next_signal_sent_at_ms:
            end_ms = min(end_ms, max(0, int(next_signal_sent_at_ms) - 1))
        return min(end_ms, now_ms)

    def _next_signal_sent_times(self, signals: list[dict[str, Any]]) -> dict[str, int | None]:
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for signal in signals:
            symbol = str(signal.get("symbol") or "")
            if not symbol:
                continue
            by_symbol.setdefault(symbol, []).append(signal)
        result: dict[str, int | None] = {}
        for symbol_signals in by_symbol.values():
            ordered = sorted(
                symbol_signals,
                key=lambda item: (int(item.get("sent_at_ms") or 0), int(item.get("audit_id") or 0)),
            )
            for idx, signal in enumerate(ordered):
                execution_id = str(signal.get("execution_id") or "")
                if not execution_id:
                    continue
                next_signal = ordered[idx + 1] if idx + 1 < len(ordered) else None
                result[execution_id] = int(next_signal.get("sent_at_ms") or 0) if next_signal else None
        return result

    def _build_signal_match_summary(
        self,
        *,
        signal: dict[str, Any],
        trades: list[FuturesTrade],
        position: PositionInfo | None,
        now_ms: int,
        next_signal_sent_at_ms: int | None = None,
    ) -> dict[str, Any]:
        direction = str(signal.get("direction") or "").lower()
        expected_side = "SELL" if direction == "short" else "BUY"
        start_ms = self._window_start_ms(signal)
        end_ms = self._bounded_window_end_ms(signal, next_signal_sent_at_ms, now_ms)
        window_trades = [trade for trade in trades if start_ms <= trade.time_ms <= end_ms]
        entry_trades, exit_trades, ignored_trades, remaining_qty = self._build_cycle_trades(window_trades, expected_side)
        cycle_trades = [*entry_trades, *exit_trades]
        avg_entry = _weighted_avg_price(entry_trades)
        avg_exit = _weighted_avg_price(exit_trades)
        planned_entry = float(signal.get("planned_entry") or signal.get("order_entry_price") or 0.0) or None
        first_entry_delay = None
        if entry_trades:
            first_entry_delay = (min(trade.time_ms for trade in entry_trades) - int(signal.get("sent_at_ms") or 0)) / 1000
        first_exit_delay = None
        holding_seconds = None
        if entry_trades and exit_trades:
            first_entry_ms = min(trade.time_ms for trade in entry_trades)
            first_exit_ms = min(trade.time_ms for trade in exit_trades)
            first_exit_delay = (first_exit_ms - int(signal.get("sent_at_ms") or 0)) / 1000
            holding_seconds = max(0.0, (max(trade.time_ms for trade in exit_trades) - first_entry_ms) / 1000)

        matched = bool(entry_trades)
        expired = now_ms > self._window_end_ms(signal)
        if next_signal_sent_at_ms and now_ms >= next_signal_sent_at_ms:
            expired = True
        status = "matched" if matched else "expired" if expired else "pending"
        cycle_status = "closed" if matched and exit_trades and remaining_qty <= 1e-9 else "open" if matched else status
        all_trade_ids = [int(trade.trade_id) for trade in cycle_trades]
        entry_trade_ids = [int(trade.trade_id) for trade in entry_trades]
        exit_trade_ids = [int(trade.trade_id) for trade in exit_trades]
        quote = sum(abs(trade.quote_qty) if trade.quote_qty else abs(trade.price * trade.qty) for trade in entry_trades)
        qty = sum(abs(trade.qty) for trade in entry_trades)
        exit_qty = sum(abs(trade.qty) for trade in exit_trades)
        maker_count = sum(1 for trade in cycle_trades if trade.is_maker)
        taker_count = len(cycle_trades) - maker_count

        return {
            "execution_id": str(signal.get("execution_id") or ""),
            "status": status,
            "cycle_status": cycle_status,
            "matched": matched,
            "symbol": str(signal.get("symbol") or ""),
            "direction": direction,
            "expected_entry_side": expected_side,
            "expected_exit_side": "BUY" if expected_side == "SELL" else "SELL",
            "signal": signal,
            "signal_sent_at_ms": int(signal.get("sent_at_ms") or 0),
            "match_window_start_ms": start_ms,
            "match_window_end_ms": self._window_end_ms(signal),
            "bounded_match_window_end_ms": end_ms,
            "next_signal_sent_at_ms": next_signal_sent_at_ms,
            "matched_until_ms": end_ms,
            "trade_signature": ",".join(str(trade_id) for trade_id in all_trade_ids),
            "entry_trade_ids": entry_trade_ids,
            "exit_trade_ids": exit_trade_ids,
            "all_trade_ids": all_trade_ids,
            "entry_trade_count": len(entry_trades),
            "exit_trade_count": len(exit_trades),
            "ignored_trade_count": len(ignored_trades),
            "window_trade_count": len(window_trades),
            "cycle_trade_count": len(cycle_trades),
            "entry_qty": qty,
            "exit_qty": exit_qty,
            "remaining_qty": max(0.0, remaining_qty),
            "entry_notional_usdc": quote,
            "exit_notional_usdc": sum(abs(trade.quote_qty) if trade.quote_qty else abs(trade.price * trade.qty) for trade in exit_trades),
            "entry_avg_price": avg_entry,
            "exit_avg_price": avg_exit,
            "planned_entry": planned_entry,
            "entry_slippage_bps": _slippage_bps(direction, planned_entry, avg_entry),
            "first_entry_delay_seconds": first_entry_delay,
            "first_exit_delay_seconds": first_exit_delay,
            "holding_seconds": holding_seconds,
            "maker_count": maker_count,
            "taker_count": taker_count,
            "cycle_realized_pnl": sum(trade.realized_pnl for trade in cycle_trades),
            "cycle_commission": sum(trade.commission for trade in cycle_trades),
            "window_realized_pnl": sum(trade.realized_pnl for trade in window_trades),
            "window_commission": sum(trade.commission for trade in window_trades),
            "position_snapshot": _position_dict(position),
            "entry_trades": [_trade_dict(trade) for trade in entry_trades[-20:]],
            "exit_trades": [_trade_dict(trade) for trade in exit_trades[-20:]],
            "ignored_trades": [_trade_dict(trade) for trade in ignored_trades[-20:]],
            "all_window_trades": [_trade_dict(trade) for trade in window_trades[-40:]],
            "checked_at_ms": now_ms,
        }

    def _build_cycle_trades(
        self,
        window_trades: list[FuturesTrade],
        expected_side: str,
    ) -> tuple[list[FuturesTrade], list[FuturesTrade], list[FuturesTrade], float]:
        entry_trades: list[FuturesTrade] = []
        exit_trades: list[FuturesTrade] = []
        ignored_trades: list[FuturesTrade] = []
        remaining_qty = 0.0
        cycle_started = False
        cycle_closed = False
        for trade in sorted(window_trades, key=lambda item: (item.time_ms, item.trade_id)):
            side = trade.side.upper()
            qty = abs(trade.qty)
            if cycle_closed:
                ignored_trades.append(trade)
                continue
            if side == expected_side:
                if cycle_started and exit_trades and remaining_qty <= 1e-9:
                    cycle_closed = True
                    ignored_trades.append(trade)
                    continue
                cycle_started = True
                entry_trades.append(trade)
                remaining_qty += qty
                continue
            if not cycle_started:
                ignored_trades.append(trade)
                continue
            exit_trades.append(trade)
            remaining_qty = max(0.0, remaining_qty - qty)
            if remaining_qty <= 1e-9:
                cycle_closed = True
        return entry_trades, exit_trades, ignored_trades, remaining_qty

    async def _record_if_changed(self, summary: dict[str, Any], previous: dict[str, Any] | None) -> None:
        status = summary["status"]
        if status == "pending" and not summary["matched"]:
            return
        if previous:
            if previous.get("status") == status and previous.get("trade_signature") == summary.get("trade_signature"):
                return
        event_type = "manual_signal_auto_matched" if summary["matched"] else "manual_signal_auto_match_expired"
        await self._audit_repo.log(event_type, "bot", summary)
        logger.info(
            event_type,
            execution_id=summary["execution_id"],
            status=status,
            symbol=summary["symbol"],
            entry_trade_count=summary["entry_trade_count"],
            entry_notional_usdc=round(float(summary["entry_notional_usdc"] or 0.0), 4),
        )
        if summary["matched"] and self._settings.manual_signal_auto_match_notify:
            previous_matched = bool(previous and previous.get("matched"))
            if not previous_matched:
                await self._notify_auto_match(summary)

    async def _notify_auto_match(self, summary: dict[str, Any]) -> None:
        if not self._telegram_app or not self._settings.telegram_chat_id_int:
            return
        signal = summary.get("signal") or {}
        slippage = summary.get("entry_slippage_bps")
        slippage_line = ""
        if slippage is not None:
            slippage_line = f"\n滑價估算：<b>{float(slippage):+.2f} bps</b>"
        await self._telegram_app.bot.send_message(
            chat_id=self._settings.telegram_chat_id_int,
            text=(
                "✅ <b>已自動匹配 mainnet 下單</b>\n"
                f"交易對：<code>{escape(str(summary['symbol']))}</code>\n"
                f"方向：<b>{escape(str(summary['direction']))}</b>\n"
                f"策略：<b>{escape(str(signal.get('strategy') or ''))}</b>\n"
                f"訊號代碼：<code>{escape(str(summary['execution_id']))}</code>\n"
                f"同向成交：<b>{summary['entry_trade_count']}</b> 筆，名目 <b>${summary['entry_notional_usdc']:.2f}</b>\n"
                f"均價：<b>${float(summary['entry_avg_price'] or 0.0):.4f}</b>，延遲：<b>{float(summary['first_entry_delay_seconds'] or 0.0):+.1f}s</b>"
                f"{slippage_line}\n"
                f"Maker/Taker：<b>{summary['maker_count']}/{summary['taker_count']}</b>\n"
                "這筆已寫入資料集，之後會拿來分析你的手動策略。"
            ),
            parse_mode="HTML",
        )
