"""Telegram bot command handlers for Testnet Live Auto Trader.

Each handler corresponds to a / command defined in spec.
All handlers receive ApplicationContext from python-telegram-bot v21.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config.settings import Settings
from src.gridbot.ai.gemini import GeminiAnalyzer
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.binance.models import FuturesTrade, IncomeRecord, PositionInfo
from src.gridbot.storage.repositories import AuditLogRepository
from src.gridbot.telegram.formatters import (
    format_testnet_dashboard,
)
from src.gridbot.telegram.lane_monitor import (
    build_lane_detail,
    build_lane_monitor,
    lane_monitor_html_chunks,
    lane_monitor_keyboard,
)
from src.gridbot.testnet.pnl import calculate_testnet_pnl_breakdown
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)
TAIPEI = ZoneInfo("Asia/Taipei")


TELEGRAM_HTML_SAFE_LIMIT = 3900


def _lane_monitor_database(context: ContextTypes.DEFAULT_TYPE):
    """Prefer the read-dedicated connection so status views cannot queue trades."""

    bot_data = context.application.bot_data
    return bot_data.get("lane_monitor_db") or bot_data.get("db")


def _telegram_html_chunks(text: str, *, limit: int = TELEGRAM_HTML_SAFE_LIMIT) -> list[str]:
    """Split HTML Telegram messages without exceeding Telegram's 4096-char limit."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    if not text:
        return [""]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current.strip():
                chunks.append(current.rstrip())
                current = ""
            # Long single lines are rare in /signal. Escape fallback chunks so a
            # forced split cannot leave an unclosed HTML tag in Telegram.
            escaped_line = escape(line)
            while len(escaped_line) > limit:
                split_at = limit
                # html.escape() emits entities such as ``&amp;``.  Splitting
                # between ``&`` and ``;`` makes Telegram reject the entire
                # parse-mode=HTML message, so move the boundary to the start of
                # the incomplete entity.  Production's 3900-char limit is far
                # longer than every entity emitted by html.escape().
                entity_start = escaped_line.rfind("&", 0, split_at)
                entity_end = escaped_line.rfind(";", 0, split_at)
                if entity_start > entity_end:
                    split_at = entity_start
                if split_at <= 0:  # Defensive fallback for impractically tiny limits.
                    split_at = limit
                chunks.append(escaped_line[:split_at])
                escaped_line = escaped_line[split_at:]
            current = escaped_line
            continue
        if current and len(current) + len(line) > limit:
            chunks.append(current.rstrip())
            current = line
        else:
            current += line
    if current.strip() or not chunks:
        chunks.append(current.rstrip())
    return chunks


async def _reply_html_chunks(message, text: str, *, limit: int = TELEGRAM_HTML_SAFE_LIMIT) -> None:
    for chunk in _telegram_html_chunks(text, limit=limit):
        await message.reply_text(chunk, parse_mode="HTML")


async def _authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings: Settings = context.application.bot_data["settings"]
    allowed_chat = settings.telegram_chat_id_int
    chat_id = update.effective_chat.id if update.effective_chat else 0
    if allowed_chat and chat_id != allowed_chat:
        logger.warning(
            "telegram_unauthorized_update_ignored",
            chat_id=chat_id,
            allowed_chat=allowed_chat,
            user_id=update.effective_user.id if update.effective_user else None,
        )
        if update.callback_query:
            await update.callback_query.answer("未授權的 Telegram chat。", show_alert=False)
        return False
    return True


def _trade_time_label(trade: FuturesTrade) -> str:
    return datetime.fromtimestamp(trade.time_ms / 1000, tz=timezone.utc).astimezone(TAIPEI).strftime("%m/%d %H:%M:%S")


async def _fetch_mainnet_trade_snapshot(
    settings: Settings,
    signal_info: dict,
) -> dict:
    if not settings.manual_mainnet_api_key or not settings.manual_mainnet_api_secret:
        return {"available": False, "reason": "manual_mainnet_api_not_configured"}

    mainnet_settings = settings.model_copy(
        update={
            "binance_api_key": settings.manual_mainnet_api_key,
            "binance_api_secret": settings.manual_mainnet_api_secret,
            "binance_testnet": False,
        }
    )
    client = BinanceFuturesClient(mainnet_settings)
    await client.connect()
    try:
        symbol = str(signal_info.get("symbol") or "")
        direction = str(signal_info.get("direction") or "").lower()
        expected_side = "BUY" if direction == "long" else "SELL"
        now = datetime.now(timezone.utc)
        lookback_start_ms = int(
            (
                now - timedelta(minutes=max(1, int(settings.manual_signal_match_window_minutes)))
            ).timestamp() * 1000
        )
        trades = await client.get_user_trades(symbol=symbol, start_time=lookback_start_ms, limit=200)
        position = await client.get_position(symbol)
        recent_trades = [trade for trade in trades if trade.side.upper() == expected_side]
        matched_qty = sum(abs(trade.qty) for trade in recent_trades)
        matched_quote = sum(abs(trade.quote_qty) if trade.quote_qty else abs(trade.price * trade.qty) for trade in recent_trades)
        avg_price = (matched_quote / matched_qty) if matched_qty > 0 else None
        return {
            "available": True,
            "symbol": symbol,
            "direction": direction,
            "expected_side": expected_side,
            "match_window_minutes": int(settings.manual_signal_match_window_minutes),
            "recent_trade_count": len(recent_trades),
            "recent_trades": [
                {
                    "trade_id": trade.trade_id,
                    "order_id": trade.order_id,
                    "side": trade.side,
                    "price": trade.price,
                    "qty": abs(trade.qty),
                    "quote_qty": abs(trade.quote_qty) if trade.quote_qty else abs(trade.price * trade.qty),
                    "time_ms": trade.time_ms,
                    "time": _trade_time_label(trade),
                    "is_maker": trade.is_maker,
                }
                for trade in recent_trades[-5:]
            ],
            "matched_qty": matched_qty,
            "matched_notional_usdc": matched_quote,
            "matched_avg_price": avg_price,
            "position": {
                "side": getattr(position, "side", "") if position else "",
                "qty": getattr(position, "qty", 0.0) if position else 0.0,
                "entry_price": getattr(position, "entry_price", 0.0) if position else 0.0,
                "mark_price": getattr(position, "mark_price", 0.0) if position else 0.0,
                "unrealized_pnl": getattr(position, "unrealized_pnl", 0.0) if position else 0.0,
            },
        }
    finally:
        await client.close()


def _format_mainnet_snapshot(snapshot: dict) -> str:
    if not snapshot.get("available"):
        return "mainnet 即時抓單：未設定 API，已先記錄這次已下單回覆。"
    lines = [
        f"mainnet 近 {snapshot['match_window_minutes']} 分鐘同向成交：{snapshot['recent_trade_count']} 筆",
        f"同向累計數量：{snapshot['matched_qty']:.6f}",
        f"同向累計名目：${snapshot['matched_notional_usdc']:.2f}",
    ]
    if snapshot.get("matched_avg_price") is not None:
        lines.append(f"同向均價：${snapshot['matched_avg_price']:.4f}")
    position = snapshot.get("position") or {}
    if position.get("qty"):
        lines.append(
            f"目前持倉：{escape(str(position.get('side') or ''))} "
            f"{float(position.get('qty') or 0.0):.6f} @ ${float(position.get('entry_price') or 0.0):.4f}"
        )
        lines.append(f"未實現損益：${float(position.get('unrealized_pnl') or 0.0):.4f}")
    recent = snapshot.get("recent_trades") or []
    if recent:
        latest = recent[-1]
        lines.append(
            f"最近一筆：{latest['time']} {latest['side']} {latest['qty']:.6f} @ ${latest['price']:.4f}"
        )
    return "\n".join(lines)


async def _latest_manual_signal_from_audit(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    db = context.application.bot_data.get("db")
    if db is None:
        return None
    row = await db.fetchone(
        "SELECT details_json FROM audit_log WHERE event_type = ? ORDER BY event_time_ms DESC LIMIT 1",
        ("manual_signal_sent",),
    )
    if not row:
        return None
    try:
        details = json.loads(row.get("details_json") or "{}")
    except json.JSONDecodeError:
        logger.warning("telegram_manual_signal_audit_decode_failed")
        return None
    return details if isinstance(details, dict) else None


def _manual_mainnet_settings(settings: Settings) -> Settings | None:
    if not settings.manual_mainnet_api_key or not settings.manual_mainnet_api_secret:
        return None
    return settings.model_copy(
        update={
            "binance_api_key": settings.manual_mainnet_api_key,
            "binance_api_secret": settings.manual_mainnet_api_secret,
            "binance_testnet": False,
        }
    )


def _position_qty(position: PositionInfo | None) -> float:
    if position is None:
        return 0.0
    for attr in ("position_amt", "qty"):
        value = getattr(position, attr, 0.0)
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


async def _fetch_mainnet_today_pnl(settings: Settings) -> dict:
    mainnet_settings = _manual_mainnet_settings(settings)
    if mainnet_settings is None:
        return {"available": False, "reason": "manual_mainnet_api_not_configured"}

    client = BinanceFuturesClient(mainnet_settings)
    await client.connect()
    try:
        now_tw = datetime.now(TAIPEI)
        day_start_tw = now_tw.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_ms = int(day_start_tw.astimezone(timezone.utc).timestamp() * 1000)
        symbols = settings.symbols_list or ["ETHUSDC"]
        result = {
            "available": True,
            "start": day_start_tw.strftime("%Y/%m/%d %H:%M:%S"),
            "end": now_tw.strftime("%Y/%m/%d %H:%M:%S"),
            "symbols": {},
            "totals": {
                "realized_pnl": 0.0,
                "commission": 0.0,
                "funding_fee": 0.0,
                "net": 0.0,
                "trade_count": 0,
                "maker_count": 0,
                "taker_count": 0,
                "maker_notional": 0.0,
                "taker_notional": 0.0,
            },
        }

        for symbol in symbols:
            incomes: list[IncomeRecord] = []
            for income_type in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                incomes.extend(
                    await client.get_income_history(
                        income_type=income_type,
                        symbol=symbol,
                        start_time=day_start_ms,
                        limit=1000,
                    )
                )
            trades = await client.get_user_trades(symbol=symbol, start_time=day_start_ms, limit=1000)
            position = await client.get_position(symbol)
            realized = sum(float(r.income) for r in incomes if r.income_type == "REALIZED_PNL")
            commission = sum(float(r.income) for r in incomes if r.income_type == "COMMISSION")
            funding = sum(float(r.income) for r in incomes if r.income_type == "FUNDING_FEE")
            maker_trades = [trade for trade in trades if trade.is_maker]
            taker_trades = [trade for trade in trades if not trade.is_maker]
            maker_notional = sum(abs(trade.quote_qty) if trade.quote_qty else abs(trade.price * trade.qty) for trade in maker_trades)
            taker_notional = sum(abs(trade.quote_qty) if trade.quote_qty else abs(trade.price * trade.qty) for trade in taker_trades)
            net = realized + commission + funding
            result["symbols"][symbol] = {
                "realized_pnl": realized,
                "commission": commission,
                "funding_fee": funding,
                "net": net,
                "trade_count": len(trades),
                "maker_count": len(maker_trades),
                "taker_count": len(taker_trades),
                "maker_notional": maker_notional,
                "taker_notional": taker_notional,
                "position": {
                    "qty": _position_qty(position),
                    "entry_price": float(getattr(position, "entry_price", 0.0) or 0.0) if position else 0.0,
                    "mark_price": float(getattr(position, "mark_price", 0.0) or 0.0) if position else 0.0,
                    "unrealized_pnl": float(getattr(position, "unrealized_pnl", 0.0) or 0.0) if position else 0.0,
                },
                "recent_trades": [
                    {
                        "time": _trade_time_label(trade),
                        "side": trade.side,
                        "price": trade.price,
                        "qty": abs(trade.qty),
                        "notional": abs(trade.quote_qty) if trade.quote_qty else abs(trade.price * trade.qty),
                        "is_maker": trade.is_maker,
                    }
                    for trade in trades[-8:]
                ],
            }
            totals = result["totals"]
            totals["realized_pnl"] += realized
            totals["commission"] += commission
            totals["funding_fee"] += funding
            totals["net"] += net
            totals["trade_count"] += len(trades)
            totals["maker_count"] += len(maker_trades)
            totals["taker_count"] += len(taker_trades)
            totals["maker_notional"] += maker_notional
            totals["taker_notional"] += taker_notional
        return result
    finally:
        await client.close()


def _format_mainnet_today_pnl(report: dict) -> str:
    if not report.get("available"):
        return "尚未設定 mainnet API，無法查詢今日 PnL。"

    totals = report["totals"]
    trade_count = int(totals["trade_count"])
    maker_count = int(totals["maker_count"])
    taker_count = int(totals["taker_count"])
    maker_ratio = (maker_count / trade_count * 100) if trade_count else 0.0
    taker_ratio = (taker_count / trade_count * 100) if trade_count else 0.0
    fee_drag = abs(float(totals["commission"]))
    realized_abs = abs(float(totals["realized_pnl"]))
    fee_vs_realized = (fee_drag / realized_abs * 100) if realized_abs > 0 else 0.0

    lines = [
        "📊 <b>Mainnet 今日 PnL</b>",
        f"時間：<code>{escape(report['start'])}</code> 到 <code>{escape(report['end'])}</code>",
        "",
        f"已實現 PnL：<b>${totals['realized_pnl']:+.4f}</b>",
        f"手續費：<b>${totals['commission']:+.4f}</b>",
        f"Funding：<b>${totals['funding_fee']:+.4f}</b>",
        f"今日淨 PnL：<b>${totals['net']:+.4f}</b>",
        "",
        f"成交數：<b>{trade_count}</b> 筆",
        f"Maker：{maker_count} 筆 ({maker_ratio:.1f}%) | Taker：{taker_count} 筆 ({taker_ratio:.1f}%)",
        f"Maker 名目：${totals['maker_notional']:.2f} | Taker 名目：${totals['taker_notional']:.2f}",
    ]
    if realized_abs > 0:
        lines.append(f"手續費 / 已實現損益幅度：{fee_vs_realized:.1f}%")

    for symbol, item in report["symbols"].items():
        lines.extend(
            [
                "",
                f"━━ <b>{escape(symbol)}</b> ━━",
                f"淨 PnL：<b>${item['net']:+.4f}</b> | 成交：{item['trade_count']} 筆",
                f"已實現：${item['realized_pnl']:+.4f} | 手續費：${item['commission']:+.4f} | Funding：${item['funding_fee']:+.4f}",
            ]
        )
        position = item.get("position") or {}
        qty = float(position.get("qty") or 0.0)
        if abs(qty) > 0:
            direction = "多單" if qty > 0 else "空單"
            lines.append(
                f"目前持倉：{direction} {abs(qty):.6f} @ ${float(position.get('entry_price') or 0.0):.4f}，"
                f"未實現：${float(position.get('unrealized_pnl') or 0.0):+.4f}"
            )

        recent = item.get("recent_trades") or []
        if recent:
            lines.append("")
            lines.append("最近成交：")
            for trade in recent[-5:]:
                maker_label = "Maker" if trade["is_maker"] else "Taker"
                lines.append(
                    f"{escape(trade['time'])} {escape(trade['side'])} "
                    f"{trade['qty']:.6f} @ ${trade['price']:.4f} "
                    f"(${trade['notional']:.2f}, {maker_label})"
                )

    warnings = []
    if taker_count:
        warnings.append(f"Taker 有 {taker_count} 筆，先檢查是不是急平倉或追價造成成本上升。")
    if fee_vs_realized >= 50:
        warnings.append("手續費相對已實現損益偏高，代表今天交易 turnover 或 taker 成本需要特別注意。")
    if totals["net"] < 0 and trade_count >= 20:
        warnings.append("今日交易數不少但淨值為負，建議回頭拆每一輪開平倉，不要只看單筆。")
    if warnings:
        lines.append("")
        lines.append("重點提醒：")
        for warning in warnings[:3]:
            lines.append(f"- {escape(warning)}")
    return "\n".join(lines)


def _compact_ai_event(event: dict) -> dict:
    details = event.get("details") or {}
    signal = details.get("signal") or details
    compact = {
        "id": event.get("id"),
        "event_type": event.get("event_type"),
        "event_time_ms": event.get("event_time_ms"),
        "execution_id": details.get("execution_id") or signal.get("execution_id"),
        "status": details.get("status"),
        "matched": details.get("matched"),
        "symbol": details.get("symbol") or signal.get("symbol"),
        "direction": details.get("direction") or signal.get("direction"),
        "strategy": signal.get("strategy"),
        "regime": signal.get("regime"),
        "market_playbook": signal.get("market_playbook"),
        "score": signal.get("score"),
        "planned_entry": details.get("planned_entry") or signal.get("planned_entry"),
        "planned_stop": signal.get("planned_stop"),
        "planned_take_profit": signal.get("planned_take_profit"),
        "range_low": signal.get("range_low"),
        "range_high": signal.get("range_high"),
        "vwap": signal.get("vwap"),
        "entry_trade_count": details.get("entry_trade_count"),
        "entry_notional_usdc": details.get("entry_notional_usdc"),
        "entry_avg_price": details.get("entry_avg_price"),
        "entry_slippage_bps": details.get("entry_slippage_bps"),
        "first_entry_delay_seconds": details.get("first_entry_delay_seconds"),
        "maker_count": details.get("maker_count"),
        "taker_count": details.get("taker_count"),
        "position_snapshot": details.get("position_snapshot"),
        "reasons": signal.get("reasons"),
    }
    trades = details.get("entry_trades") or details.get("all_window_trades") or []
    compact["recent_matched_trades"] = trades[-8:]
    return {k: v for k, v in compact.items() if v is not None}


async def _latest_ai_context(context: ContextTypes.DEFAULT_TYPE) -> dict:
    db = context.application.bot_data.get("db")
    if db is None:
        return {"events": []}
    rows = await db.fetchall(
        """SELECT id, event_time_ms, event_type, details_json
        FROM audit_log
        WHERE event_type IN (?, ?, ?, ?)
        ORDER BY id DESC
        LIMIT 12""",
        (
            "manual_signal_auto_matched",
            "manual_signal_auto_match_expired",
            "manual_signal_confirmed",
            "manual_signal_sent",
        ),
    )
    events = []
    for row in rows:
        try:
            details = json.loads(row.get("details_json") or "{}")
        except json.JSONDecodeError:
            details = {}
        events.append(
            _compact_ai_event(
                {
                    "id": row.get("id"),
                    "event_time_ms": row.get("event_time_ms"),
                    "event_type": row.get("event_type"),
                    "details": details,
                }
            )
        )
    return {
        "generated_at_taipei": datetime.now(TAIPEI).strftime("%Y/%m/%d %H:%M:%S"),
        "events": events,
    }


def _call_minimax_sync(settings: Settings, payload: dict) -> str:
    body = {
        "model": settings.minimax_model or "MiniMax-M3",
        "temperature": 0.2,
        "max_tokens": 900,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是加密貨幣合約短線交易執行分析師。"
                    "只能根據使用者提供的 JSON 事件分析，不要臆測不存在的成交。"
                    "用繁體中文，輸出要短、可執行，包含：目前判斷、是否適合手動做、如果已成交則檢查進場品質、風險與下一步。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ],
    }
    req = urllib.request.Request(
        (settings.minimax_base_url or "https://api.minimax.io/v1").rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.minimax_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data["choices"][0]["message"]["content"]).strip()


async def _call_minimax(settings: Settings, payload: dict) -> str:
    return await asyncio.to_thread(_call_minimax_sync, settings, payload)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — Welcome message."""
    if not await _authorized(update, context):
        return
    await update.message.reply_text(
        "🟢 <b>Cry3 手動訊號模式已啟動</b>\n\n"
        "系統目前會在 GCP VM 背景持續掃描 ETHUSDC，出現可執行訊號時主動推送 Telegram。\n"
        "你看到開單通知後，去 mainnet 下單；系統會自動抓 mainnet 成交來匹配該訊號。\n"
        "訊息下方的 <b>已下單</b> 按鈕保留作人工補註記，不按也會收集資料。\n\n"
        "輸入 /signal 可查看最新判斷，輸入 /mainnet 可管理 one-run 驗證，輸入 /help 看完整流程。",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — List all active commands."""
    if not await _authorized(update, context):
        return
    await update.message.reply_text(
        "📋 <b>目前 Telegram 流程</b>\n\n"
        "➡️ /signal — 查看目前 ETHUSDC 最新訊號判斷\n"
        "➡️ /mainnet — 啟動/查詢 mainnet one-run 自動驗證\n"
        "➡️ /pnl — 查看 mainnet 今日 PnL 與成交分析\n"
        "➡️ /pause — 暫停訊號推送\n"
        "➡️ /resume — 恢復訊號推送\n"
        "➡️ /start — 查看目前模式\n"
        "➡️ /help — 顯示此說明\n\n"
        "自動流程：\n"
        "1. Bot 偵測到可開單訊號時，會主動推送 <b>開單通知</b>。\n"
        "2. 你在 mainnet 完成下單後，Bot 會自動抓成交並匹配該則訊號。\n"
        "3. <b>已下單</b> 按鈕只作人工補註記；不按也會寫入資料集。\n"
        "4. /mainnet 的 one-run 只會在你按下啟動後接下一個 wildcat 訊號，完成後自動停止。\n"
        "5. 之後會用這些 signal / 成交 / PnL 配對資料分析並優化策略。\n\n"
        "目前 /testnet 已停用；/pnl 會查 mainnet 今日 PnL。",
        parse_mode="HTML",
    )


async def cmd_testnet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/testnet — Testnet trader status and guardrails."""
    if not await _authorized(update, context):
        return
    app_data = context.application.bot_data
    settings: Settings = app_data["settings"]
    if not settings.testnet_legacy_enabled:
        await update.message.reply_text("ℹ️ 舊 Testnet 功能已停用；請使用 /status、/mainnet 或 /pnl。")
        return
    binance_client: BinanceFuturesClient = app_data["binance_client"]

    if settings.testnet_telegram_signal_only:
        await update.message.reply_text("ℹ️ 目前為手動訊號模式，這個 bot 不會自動下 testnet 單；主要請看 Telegram 開單通知與 /signal。")
        return

    await update.message.reply_text("⏳ 正在取得 testnet 狀態...")

    try:
        from datetime import datetime, timezone

        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_ms = int(day_start.timestamp() * 1000)

        account = await binance_client.get_account_info()
        positions: dict[str, PositionInfo | None] = {}
        open_orders: dict[str, list[dict]] = {}
        today_income: dict[str, list[IncomeRecord]] = {}
        today_trades: dict[str, list] = {}
        commission_rates: dict[str, dict] = {}

        for symbol in settings.symbols_list:
            positions[symbol] = await binance_client.get_position(symbol)
            open_orders[symbol] = await binance_client.get_open_orders(symbol)
            commission_rates[symbol] = await binance_client.get_commission_rate(symbol)
            today_trades[symbol] = await binance_client.get_user_trades(
                symbol=symbol,
                start_time=day_start_ms,
                limit=1000,
            )
            records: list[IncomeRecord] = []
            for income_type in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                records.extend(
                    await binance_client.get_income_history(
                        income_type=income_type,
                        symbol=symbol,
                        start_time=day_start_ms,
                        limit=100,
                    )
                )
            today_income[symbol] = records

        report = format_testnet_dashboard(
            settings=settings,
            account=account,
            positions=positions,
            open_orders=open_orders,
            today_income=today_income,
            commission_rates=commission_rates,
            today_trades=today_trades,
        )
        await update.message.reply_text(report, parse_mode="HTML")

    except Exception as exc:
        logger.error("cmd_testnet_failed", error=str(exc))
        await update.message.reply_text(f"❌ 取得 testnet 狀態失敗：{str(exc)[:300]}")


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pnl — Mainnet today PnL and recent trade structure."""
    if not await _authorized(update, context):
        return
    app_data = context.application.bot_data
    settings: Settings = app_data["settings"]

    await update.message.reply_text("⏳ 正在查詢 mainnet 今日 PnL...")

    try:
        report = await _fetch_mainnet_today_pnl(settings)
        await update.message.reply_text(_format_mainnet_today_pnl(report), parse_mode="HTML")

    except Exception as exc:
        logger.error("cmd_mainnet_pnl_failed", error=str(exc))
        await update.message.reply_text(f"❌ 取得 mainnet PnL 失敗：{str(exc)[:300]}")


async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ai — MiniMax-M3 analysis of recent manual signals and matched trades."""
    if not await _authorized(update, context):
        return
    settings: Settings = context.application.bot_data["settings"]
    if not settings.minimax_api_key:
        await update.message.reply_text("尚未設定 MiniMax API key，無法執行 AI 分析。")
        return
    await update.message.reply_text("⏳ 正在用 MiniMax-M3 分析最新 signal / 成交配對...")
    try:
        payload = await _latest_ai_context(context)
        if not payload.get("events"):
            await update.message.reply_text("目前還沒有可分析的 signal / 成交配對資料。")
            return
        analysis = await _call_minimax(settings, payload)
        if len(analysis) > 3800:
            analysis = analysis[:3800] + "\n\n（內容較長，已截斷）"
        await update.message.reply_text(f"🧠 <b>MiniMax-M3 交易分析</b>\n\n{escape(analysis)}", parse_mode="HTML")
    except Exception as exc:
        logger.error("cmd_ai_failed", error=str(exc))
        await update.message.reply_text(f"❌ MiniMax-M3 分析失敗：{str(exc)[:300]}")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pause — Pause scheduled fetching and trading."""
    if not await _authorized(update, context):
        return
    scheduler = context.application.bot_data.get("scheduler")
    if scheduler:
        scheduler.pause()
        await update.message.reply_text("⏸️ 已暫停訊號推送。VM 仍在線，但暫時不會主動送出開單通知。")
    else:
        await update.message.reply_text("排程器未初始化")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/resume — Resume scheduled fetching and trading."""
    if not await _authorized(update, context):
        return
    scheduler = context.application.bot_data.get("scheduler")
    if scheduler:
        scheduler.resume()
        await update.message.reply_text("▶️ 已恢復訊號推送。系統會繼續掃描 ETHUSDC，有可開單訊號會主動通知。")
    else:
        await update.message.reply_text("排程器未初始化")


def _rescue_menu(current: bool) -> tuple[str, InlineKeyboardMarkup]:
    """Build the /rescue status text + inline keyboard for the given state."""
    state = "✅ 開啟" if current else "⛔ 關閉"
    text = (
        "🚑 <b>Rescue / Catch-up 追進度模式</b>\n"
        f"目前狀態：<b>{state}</b>\n\n"
        "<b>開啟</b>：<u>立即</u>進入搶單模式（不必等中午，只要當日未達標就放寬"
        "進場條件搶單，回測顯示是主要獲利來源）。\n"
        "<b>關閉</b>：停止，只用嚴格 S1/S2/S5 訊號，交易量大減但每筆品質較高。\n\n"
        "💡 半夜低流動性時段建議關閉，避免品質差的單。\n"
        "點下方按鈕切換（即時生效，無需重啟）："
    )
    buttons = [[
        InlineKeyboardButton("🟢 開啟 ✓" if current else "開啟", callback_data="rescue:on"),
        InlineKeyboardButton("關閉" if current else "🔴 關閉 ✓", callback_data="rescue:off"),
    ]]
    return text, InlineKeyboardMarkup(buttons)


async def cmd_rescue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rescue — Show the catch-up/rescue toggle menu (inline buttons).

    The setting is persisted in app_config and read live by the mainnet one-run
    manager, so toggling takes effect without a restart.
    """
    if not await _authorized(update, context):
        return
    from src.gridbot.mainnet.one_run import RESCUE_CONFIG_KEY

    config_repo = context.application.bot_data.get("config_repo")
    if config_repo is None:
        await update.message.reply_text("❌ 設定儲存未初始化（mainnet 未啟用）。")
        return

    raw = await config_repo.get(RESCUE_CONFIG_KEY)
    current = raw != "0"  # default enabled
    text, markup = _rescue_menu(current)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def handle_rescue_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the rescue:on / rescue:off inline buttons from /rescue."""
    if not await _authorized(update, context):
        return
    query = update.callback_query
    if query is None:
        return
    from src.gridbot.mainnet.one_run import RESCUE_CONFIG_KEY

    config_repo = context.application.bot_data.get("config_repo")
    if config_repo is None:
        await query.answer("設定儲存未初始化（mainnet 未啟用）。", show_alert=True)
        return
    manager = context.application.bot_data.get("mainnet_one_run_manager")
    target = (query.data or "") == "rescue:on"

    # Prefer the manager setter so its in-memory cache stays in sync.
    if manager is not None:
        await manager.set_rescue_enabled(target)
    else:
        await config_repo.set(RESCUE_CONFIG_KEY, "1" if target else "0")

    await query.answer("已開啟 Rescue" if target else "已關閉 Rescue")
    text, markup = _rescue_menu(target)
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception as exc:  # noqa: BLE001 — e.g. "message is not modified"
        logger.info("rescue_menu_edit_skipped", error=str(exc))


async def _build_codex_signal_snapshot(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    execution_wired: bool,
) -> str | None:
    manager = context.application.bot_data.get("mainnet_one_run_manager")
    if manager is None:
        return None

    try:
        from scripts.backtest_wildcat_s1s5 import build_features
        from src.gridbot.strategy.codex_v1_live import (
            build_codex_v1_live_features,
            describe_codex_v1_nearest_lanes,
            format_codex_v1_telegram_report,
        )
        from src.gridbot.strategy.wildcat_live import (
            explain_wildcat_no_signal,
            generate_wildcat_v2_adverse_guard_live_decision,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cmd_signal_import_failed", error=str(exc))
        return None

    settings: Settings = context.application.bot_data["settings"]
    candles = await manager._load_candles(settings.mainnet_symbol)
    if len(candles) < 160:
        return (
            "⚡ <b>即時 lane snapshot</b>\n"
            f"目前 K 線不足：<code>{len(candles)}</code> 根，至少需要 <code>160</code> 根。"
        )

    rng15 = 0.0
    if len(candles) >= 16:
        window = candles[-16:-1]
        hi = max(candle.high for candle in window)
        lo = min(candle.low for candle in window)
        px = candles[-1].close
        rng15 = (hi - lo) / px * 1e4 if px > 0 else 0.0
    drift_bp = manager._signed_drift_bp(candles, settings.mainnet_range_drift_window_bars)
    rescue_enabled = await manager._is_rescue_enabled()
    decision = generate_wildcat_v2_adverse_guard_live_decision(
        candles,
        target_daily_usdc=settings.mainnet_equity_cap_usdc * 0.03,
        notional_usdc=settings.mainnet_effective_entry_notional_usdc,
        leverage=settings.mainnet_leverage,
        rescue_enabled=rescue_enabled,
    )

    if decision is None:
        reasons = explain_wildcat_no_signal(
            candles,
            target_daily_usdc=settings.mainnet_equity_cap_usdc * 0.03,
            leverage=settings.mainnet_leverage,
        )
        detail = "\n".join(reasons[:3]) if reasons else "目前 wildcat 沒有形成候選訊號。"
        return (
            "⚡ <b>即時 lane snapshot</b>\n"
            f"  • 最新 K：<code>{datetime.fromtimestamp((candles[-1].open_time_ms + 60_000) / 1000, tz=timezone.utc).astimezone(TAIPEI).strftime('%Y/%m/%d %H:%M:%S')}</code>\n"
            "  • wildcat：<code>no_candidate</code>\n"
            f"{detail}"
        )

    raw = [
        {
            "time_ms": candle.open_time_ms,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
            "quote_volume": candle.quote_volume,
        }
        for candle in candles
    ]
    feature_series = None
    try:
        feature_series = build_features(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cmd_signal_feature_series_failed", error=str(exc))

    features = build_codex_v1_live_features(
        symbol=settings.mainnet_symbol,
        strategy=decision.strategy,
        side=decision.side,
        score=decision.signal.score,
        rng15=rng15,
        d30=drift_bp,
        signal=decision.signal,
        candles=raw,
        feature_series=feature_series,
    )
    nearest = describe_codex_v1_nearest_lanes(features, limit=3)
    nearest_block = "\n".join(f"  • {escape(line)}" for line in nearest)
    latest_bar_time = datetime.fromtimestamp(
        (candles[-1].open_time_ms + 60_000) / 1000,
        tz=timezone.utc,
    ).astimezone(TAIPEI).strftime("%Y/%m/%d %H:%M:%S")
    head = [
        "⚡ <b>即時 lane snapshot</b>",
        f"  • 最新 K：<code>{escape(latest_bar_time)}</code>",
        (
            "  • wildcat candidate: "
            f"<code>{escape(decision.strategy)}</code> / "
            f"<code>{escape(decision.side)}</code> / "
            f"score=<code>{float(decision.signal.score):.1f}</code>"
        ),
        f"  • rescue：<code>{'ON' if rescue_enabled else 'OFF'}</code>",
        "",
    ]
    return "\n".join(head) + format_codex_v1_telegram_report(features, execution_wired=execution_wired) + "\n\n🎯 <b>最近 lane / 尚差門檻</b>\n" + nearest_block


async def _build_codex_signal_stats(context: ContextTypes.DEFAULT_TYPE) -> str:
    db = context.application.bot_data.get("db")
    if db is None:
        return "📊 <b>Codex gate 統計</b>\n  • DB 未初始化。"

    now_tpe = datetime.now(tz=TAIPEI)
    start_tpe = now_tpe.replace(hour=0, minute=0, second=0, microsecond=0)
    since_ms = int(start_tpe.astimezone(timezone.utc).timestamp() * 1000)
    now_ms = int(now_tpe.astimezone(timezone.utc).timestamp() * 1000)
    current_version_fragment = "v1.4"
    # Keep the operator-facing identity tied to the imported strategy runtime;
    # a hard-coded copy previously drifted all the way back to v1.4.2.
    runtime_version = CODEX_V1_VERSION
    schema_version = "2026_06_20_v133"
    settings = context.application.bot_data.get("settings")
    config_keys = (
        "mainnet_strategy_label",
        "mainnet_codex_v1_enabled",
        "mainnet_codex_v1_w2a_shadow_only_enabled",
        "mainnet_codex_v1_w6a_guarded_200cap_enabled",
        "mainnet_codex_tp_policy_live_override_enabled",
        "mainnet_codex_v133_no_lane_miner_enabled",
        "mainnet_codex_v133_shadow_family_quota_enabled",
        "mainnet_codex_v133_diagnostic_fill_enabled",
        "mainnet_codex_v133_tp_terminalization_enabled",
        "mainnet_codex_v133_fee_gate_audit_only",
        "mainnet_codex_v133_fee_gate_enforce",
        "mainnet_codex_v133_net_floor_audit_only",
        "mainnet_codex_v133_maker_first_profit_exit",
    )
    config_snapshot = {key: getattr(settings, key, None) for key in config_keys} if settings is not None else {}
    config_hash = hashlib.sha1(
        json.dumps(config_snapshot, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:8] if config_snapshot else "n/a"

    event_types = (
        "entry_codex_v1_accepted",
        "entry_codex_v1_skipped",
        "entry_codex_v1_hard_blocked",
        "entry_codex_v1_no_lane_candidate",
        "entry_codex_v1_shadow_sample_started",
        "entry_codex_v1_shadow_sample_dropped",
        "entry_codex_v1_shadow_outcome",
        "entry_codex_v1_tp_policy_shadow_started",
        "entry_codex_v1_tp_policy_shadow_outcome",
        "entry_codex_v1_tp_policy_shadow_dropped",
        "w6a_exit_policy_shadow",
        "entry_placed",
        "entry_filled",
        "completed",
    )
    event_placeholders = ", ".join("?" for _ in event_types)
    events_all = await db.fetchall(
        f"""SELECT run_id, event_type, details_json, event_time_ms
        FROM mainnet_run_events
        WHERE event_time_ms >= ?
          AND event_time_ms <= ?
          AND event_type IN ({event_placeholders})
        ORDER BY event_time_ms ASC
        LIMIT 10000""",
        (since_ms, now_ms, *event_types),
    )
    runs_all = await db.fetchall(
        """SELECT run_id, status, exit_reason, signal_json, armed_at_ms, completed_at_ms,
                  realized_pnl_usdc, commission_usdc, side, cumulative_notional_usdc
        FROM mainnet_runs
        WHERE armed_at_ms >= ?
        ORDER BY armed_at_ms ASC
        LIMIT 300""",
        (since_ms,),
    )

    def _json_loads(raw: object) -> dict:
        try:
            data = json.loads(str(raw or "{}"))
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    parsed_events: list[dict] = []
    version_counts: dict[str, int] = {}
    for event in events_all:
        raw_json = str(event.get("details_json") or "")
        details = _json_loads(raw_json)
        event_type = str(event.get("event_type") or "")
        details["event_type"] = event_type
        version = str(
            details.get("version")
            or (details.get("decision") or {}).get("version")
            or (details.get("effective_execution") or {}).get("version")
            or (details.get("raw_classifier") or {}).get("version")
            or (current_version_fragment if current_version_fragment in raw_json else "unknown")
        )
        version_counts[version] = version_counts.get(version, 0) + 1
        parsed_events.append({**dict(event), "details": details, "raw_json": raw_json, "version": version})

    version_scoped_indices = {
        idx
        for idx, event in enumerate(parsed_events)
        if current_version_fragment in str(event.get("version") or event.get("raw_json") or "")
    }
    scoped_run_ids = {
        str(parsed_events[idx].get("run_id") or "")
        for idx in version_scoped_indices
        if parsed_events[idx].get("run_id")
    }
    scoped_runs = [
        row
        for row in runs_all
        if str(row.get("run_id") or "") in scoped_run_ids
        or current_version_fragment in str(row.get("signal_json") or "")
    ]
    scoped_run_ids.update(str(row.get("run_id") or "") for row in scoped_runs if row.get("run_id"))
    if version_scoped_indices or scoped_run_ids:
        scoped_events = [
            event
            for idx, event in enumerate(parsed_events)
            if idx in version_scoped_indices or str(event.get("run_id") or "") in scoped_run_ids
        ]
        scope_label = current_version_fragment
    else:
        scoped_events = parsed_events
        scope_label = "all_codex_today"
        scoped_run_ids = {str(event.get("run_id") or "") for event in scoped_events if event.get("run_id")}
        scoped_runs = [row for row in runs_all if str(row.get("run_id") or "") in scoped_run_ids]

    accepted_events = sum(1 for event in scoped_events if event.get("event_type") == "entry_codex_v1_accepted")
    entry_placed_events = sum(1 for event in scoped_events if event.get("event_type") == "entry_placed")
    entry_filled_events = sum(1 for event in scoped_events if event.get("event_type") == "entry_filled")
    skip_events = [event for event in scoped_events if event.get("event_type") in {"entry_codex_v1_skipped", "entry_codex_v1_hard_blocked"}]
    no_lane_candidates = [event for event in scoped_events if event.get("event_type") == "entry_codex_v1_no_lane_candidate"]
    shadow_starts = [event for event in scoped_events if event.get("event_type") == "entry_codex_v1_shadow_sample_started"]
    shadow_drops = [event for event in scoped_events if event.get("event_type") == "entry_codex_v1_shadow_sample_dropped"]
    shadow_outcomes = [event for event in scoped_events if event.get("event_type") == "entry_codex_v1_shadow_outcome"]
    tp_policy_starts = [event for event in scoped_events if event.get("event_type") == "entry_codex_v1_tp_policy_shadow_started"]
    tp_policy_outcomes = [event for event in scoped_events if event.get("event_type") == "entry_codex_v1_tp_policy_shadow_outcome"]
    tp_policy_drops = [event for event in scoped_events if event.get("event_type") == "entry_codex_v1_tp_policy_shadow_dropped"]

    def _bump(counts: dict[str, int], key: object) -> None:
        name = str(key or "-")
        if name and name != "-":
            counts[name] = counts.get(name, 0) + 1

    def _nested(data: dict, key: str) -> dict:
        value = data.get(key)
        return value if isinstance(value, dict) else {}

    def _event_fields(details: dict) -> tuple[str, str, str, str, str, str]:
        raw = _nested(details, "raw_classifier")
        effective = _nested(details, "effective_execution")
        decision = _nested(details, "decision")
        metrics = _nested(decision, "metrics")
        lane = (
            details.get("lane_code")
            or effective.get("lane_code")
            or raw.get("lane_code")
            or decision.get("lane_code")
            or "NONE"
        )
        shadow_lane = (
            details.get("shadow_lane")
            or decision.get("shadow_lane")
            or metrics.get("shadow_lane")
            or ""
        )
        candidate_lane = details.get("candidate_bucket") or details.get("candidate_lane") or decision.get("candidate_lane") or ""
        reason = (
            effective.get("effective_reason")
            or details.get("shadow_outcome")
            or details.get("terminal_reason")
            or details.get("reason")
            or decision.get("reason")
            or "-"
        )
        policy = (
            details.get("policy_tag")
            or decision.get("policy_tag")
            or metrics.get("policy_tag")
            or metrics.get("policy_note")
            or shadow_lane
            or candidate_lane
            or "-"
        )
        status = effective.get("status") or details.get("effective_status") or "-"
        return str(lane), str(status), str(reason), str(policy), str(shadow_lane), str(candidate_lane)

    reason_rows: dict[str, int] = {}
    reason_runs: dict[str, set[str]] = {}
    lane_rows: dict[str, int] = {}
    side_reason_rows: dict[str, int] = {}
    for event in skip_events:
        details = event["details"]
        lane, _status, reason, _policy, shadow_lane, _candidate_lane = _event_fields(details)
        run_id = str(event.get("run_id") or "-")
        _bump(reason_rows, reason)
        reason_runs.setdefault(reason, set()).add(run_id)
        _bump(lane_rows, shadow_lane or lane)
        decision = _nested(details, "decision")
        effective = _nested(details, "effective_execution")
        features = _nested(effective, "features")
        side = decision.get("side") or effective.get("side") or features.get("side") or "-"
        strategy = decision.get("strategy") or effective.get("strategy") or features.get("strategy") or "-"
        _bump(side_reason_rows, f"{side}/{strategy}/{reason}")

    no_lane_bucket_counts: dict[str, int] = {}
    shadow_start_counts: dict[str, int] = {}
    shadow_drop_counts: dict[str, int] = {}
    shadow_outcome_counts: dict[str, int] = {}
    shadow_family_counts: dict[str, int] = {}
    shadow_metrics: dict[str, dict[str, Any]] = {}
    shadow_pending_ids: set[str] = set()
    shadow_done_ids: set[str] = set()
    shadow_opportunity_ids: set[str] = set()
    missing_opportunity_rows = 0

    def _shadow_group(details: dict, lane: str, shadow_lane: str, candidate_lane: str) -> str:
        return str(shadow_lane or details.get("candidate_bucket") or candidate_lane or lane or details.get("reason") or "-")

    def _shadow_family(details: dict, group: str) -> str:
        family = str(details.get("shadow_lane_family") or "")
        if family:
            return family
        if group.startswith("SH_W2A"):
            return "W2A"
        if group.startswith("SH_W6A"):
            return "W6A"
        if group.startswith("SH_ANCHOR_S") or group == "ANCHOR-S":
            return "ANCHOR_S"
        if group.startswith("SH_DISABLED"):
            return "DISABLED"
        if group.startswith("SH_SHORT"):
            return "SHORT_VETO"
        if group.startswith("SH_") or group.startswith("NL-"):
            return "NL"
        return "OTHER"

    for event in no_lane_candidates:
        details = event["details"]
        _bump(no_lane_bucket_counts, details.get("candidate_bucket") or details.get("nearest_lane_code") or "NL_UNCLASSIFIED")

    for event in shadow_starts:
        details = event["details"]
        lane, _status, reason, _policy, shadow_lane, candidate_lane = _event_fields(details)
        sample_id = str(details.get("sample_id") or "")
        opportunity_id = str(details.get("opportunity_id") or "")
        if sample_id:
            shadow_pending_ids.add(sample_id)
        if opportunity_id:
            shadow_opportunity_ids.add(opportunity_id)
        else:
            missing_opportunity_rows += 1
        group = _shadow_group(details, lane, shadow_lane, candidate_lane)
        _bump(shadow_start_counts, group or reason)
        _bump(shadow_family_counts, _shadow_family(details, group))
    for event in shadow_drops:
        details = event["details"]
        lane, _status, reason, _policy, shadow_lane, candidate_lane = _event_fields(details)
        opportunity_id = str(details.get("opportunity_id") or "")
        if opportunity_id:
            shadow_opportunity_ids.add(opportunity_id)
        else:
            missing_opportunity_rows += 1
        group = _shadow_group(details, lane, shadow_lane, candidate_lane)
        drop_reason = str(details.get("drop_reason") or "dropped")
        _bump(shadow_drop_counts, f"{group}:{drop_reason}")
        _bump(shadow_family_counts, _shadow_family(details, group))
    for event in shadow_outcomes:
        details = event["details"]
        lane, _status, _reason, _policy, shadow_lane, candidate_lane = _event_fields(details)
        sample_id = str(details.get("sample_id") or "")
        opportunity_id = str(details.get("opportunity_id") or "")
        if sample_id:
            shadow_done_ids.add(sample_id)
        if opportunity_id:
            shadow_opportunity_ids.add(opportunity_id)
        else:
            missing_opportunity_rows += 1
        outcome = str(details.get("shadow_outcome") or details.get("outcome") or "-")
        group = _shadow_group(details, lane, shadow_lane, candidate_lane)
        family = _shadow_family(details, group)
        _bump(shadow_outcome_counts, f"{group}:{outcome}")
        _bump(shadow_family_counts, family)
        promotion_counts_as = str(details.get("promotion_counts_as") or "")
        if (
            promotion_counts_as in {"excluded_terminal", "diagnostic_only"}
            or details.get("diagnostic_only")
            or details.get("promotion_eligible") is False
        ):
            continue
        metrics = shadow_metrics.setdefault(
            group,
            {"n": 0, "tp1_first": 0, "sl_first": 0, "no_fill": 0, "none": 0, "ambiguous_both": 0, "pnl": 0.0, "families": set()},
        )
        metrics["n"] += 1
        metrics[outcome] = int(metrics.get(outcome, 0)) + 1
        metrics["families"].add(family)
        try:
            metrics["pnl"] += float(details.get("paper_pnl_bp_after_fee") or 0.0)
        except (TypeError, ValueError):
            pass
    pending_shadow = max(0, len(shadow_pending_ids - shadow_done_ids))

    run_status_counts: dict[str, int] = {}
    for row in scoped_runs:
        _bump(run_status_counts, f"{row.get('status')}/{row.get('exit_reason') or '-'}")
    realized = sum(float(row.get("realized_pnl_usdc") or 0) for row in scoped_runs)
    fees = sum(float(row.get("commission_usdc") or 0) for row in scoped_runs)
    net = realized - fees
    traded_runs = [row for row in scoped_runs if float(row.get("cumulative_notional_usdc") or 0) > 0]

    def _top_pairs(counts: dict[str, int], limit: int = 5) -> str:
        if not counts:
            return "-"
        return ", ".join(
            f"{escape(name)}={count}" for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
        )

    def _top_reason_pairs(limit: int = 6) -> str:
        if not reason_rows:
            return "-"
        parts: list[str] = []
        for reason, rows in sorted(reason_rows.items(), key=lambda item: (-item[1], item[0]))[:limit]:
            parts.append(f"{escape(reason)}={rows}/{len(reason_runs.get(reason, set()))}r")
        return ", ".join(parts)

    def _promotion_readiness(limit: int = 5) -> str:
        if not shadow_metrics:
            return "-"
        rows: list[str] = []
        for lane, metrics in sorted(shadow_metrics.items(), key=lambda item: (-int(item[1].get("n") or 0), item[0]))[:limit]:
            n = int(metrics.get("n") or 0)
            if n <= 0:
                continue
            tp = int(metrics.get("tp1_first") or 0)
            sl = int(metrics.get("sl_first") or 0) + int(metrics.get("ambiguous_both") or 0)
            nf = int(metrics.get("no_fill") or 0)
            tp_rate = tp / n * 100.0
            sl_rate = sl / n * 100.0
            nf_rate = nf / n * 100.0
            pnl = float(metrics.get("pnl") or 0.0)
            status = "READY?" if n >= 50 and tp_rate >= 65.0 and sl_rate <= 25.0 and nf_rate <= 15.0 and pnl > 0 else "collecting"
            rows.append(f"{escape(lane)} {n}/50 TP={tp_rate:.1f}% SL={sl_rate:.1f}% NF={nf_rate:.1f}% pnl_bp={pnl:+.1f} {status}")
        return "; ".join(rows) or "-"

    def _data_quality_warnings() -> str:
        warnings: list[str] = []
        ambiguous = sum(1 for event in shadow_outcomes if str(event["details"].get("shadow_outcome") or "") == "ambiguous_both")
        cooldown_drops = sum(1 for event in shadow_drops if str(event["details"].get("drop_reason") or "") == "cooldown")
        cap_drops = sum(1 for event in shadow_drops if str(event["details"].get("drop_reason") or "") == "per_run_cap")
        if missing_opportunity_rows:
            warnings.append(f"missing_opp={missing_opportunity_rows}")
        if ambiguous:
            warnings.append(f"ambiguous={ambiguous}")
        if cooldown_drops:
            warnings.append(f"cooldown_drop={cooldown_drops}")
        if cap_drops:
            warnings.append(f"cap_drop={cap_drops}")
        return ", ".join(warnings) or "ok"

    tp_policy_paired_ids = {str(event["details"].get("paired_sample_id") or "") for event in tp_policy_outcomes if event["details"].get("paired_sample_id")}
    tp_policy_mismatch_by_sample: dict[str, int] = {}
    for event in tp_policy_outcomes:
        paired_id = str(event["details"].get("paired_sample_id") or "")
        if not paired_id:
            continue
        try:
            mismatch_count = int(event["details"].get("tp1_touch_mismatch_count") or 0)
        except (TypeError, ValueError):
            mismatch_count = 0
        tp_policy_mismatch_by_sample[paired_id] = max(tp_policy_mismatch_by_sample.get(paired_id, 0), mismatch_count)
    tp_policy_mismatch = sum(1 for mismatch_count in tp_policy_mismatch_by_sample.values() if mismatch_count > 0)
    tp_policy_pending = max(0, len(tp_policy_starts) - len(tp_policy_paired_ids) - len(tp_policy_drops))
    tp_policy_drift_values: list[float] = []
    tp_policy_groups: dict[str, dict[str, Any]] = {}
    for event in tp_policy_outcomes:
        details = event["details"]
        policy_id = str(details.get("tp_policy_id") or "")
        if not policy_id or policy_id == "baseline":
            continue
        if details.get("primary_promotion_eligible") is False:
            continue
        lane = str(details.get("lane_family") or details.get("shadow_lane_family") or details.get("candidate_lane") or "UNKNOWN")
        key = f"{lane} {policy_id}"
        group = tp_policy_groups.setdefault(key, {"n": 0, "delta": 0.0, "deltas": [], "beats": 0, "capture": []})
        try:
            delta = float(details.get("delta_vs_baseline_bp_after_fee") or 0.0)
        except (TypeError, ValueError):
            delta = 0.0
        group["n"] += 1
        group["delta"] += delta
        group["deltas"].append(delta)
        if details.get("beats_baseline"):
            group["beats"] += 1
        try:
            group["capture"].append(float(details.get("mfe_capture_ratio") or 0.0) * 100.0)
        except (TypeError, ValueError):
            pass
        drift = details.get("baseline_simulator_drift_bp")
        if drift is not None:
            try:
                tp_policy_drift_values.append(abs(float(drift)))
            except (TypeError, ValueError):
                pass

    def _median(values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    def _tp_policy_summary(limit: int = 3) -> str:
        if not tp_policy_groups:
            return "-"
        rows: list[str] = []
        for name, group in sorted(tp_policy_groups.items(), key=lambda item: (-float(item[1].get("delta") or 0.0), item[0]))[:limit]:
            n = int(group.get("n") or 0)
            beat = (int(group.get("beats") or 0) / n * 100.0) if n else 0.0
            med = _median([float(v) for v in group.get("deltas") or []])
            cap = _median([float(v) for v in group.get("capture") or []])
            rows.append(f"{escape(name)} N={n} Δ={float(group.get('delta') or 0.0):+.1f}bp med={med:+.1f} beat={beat:.0f}% cap={cap:.0f}%")
        return "; ".join(rows) or "-"

    def _tp_policy_quality() -> str:
        parts: list[str] = []
        parts.append("tp1=ok" if tp_policy_mismatch == 0 else f"tp1_mismatch={tp_policy_mismatch}")
        if tp_policy_drift_values:
            avg_drift = sum(tp_policy_drift_values) / len(tp_policy_drift_values)
            parts.append(f"drift={avg_drift:.1f}bp")
        else:
            parts.append("drift=n/a")
        if tp_policy_drops:
            parts.append(f"dropped={len(tp_policy_drops)}")
        return ", ".join(parts)

    def _latest_time(events: list[dict]) -> str:
        values = [int(event.get("event_time_ms") or 0) for event in events if int(event.get("event_time_ms") or 0) > 0]
        if not values:
            return "n/a"
        return datetime.fromtimestamp(max(values) / 1000.0, tz=timezone.utc).astimezone(TAIPEI).strftime("%H:%M:%S")

    def _evidence_integrity() -> str:
        no_lane_raw = reason_rows.get("no_codex_v1_lane_match", 0)
        classified_rate = (len(no_lane_candidates) / no_lane_raw * 100.0) if no_lane_raw else 0.0
        diagnostic_leaks = sum(
            1
            for event in [*shadow_starts, *shadow_outcomes]
            if event["details"].get("diagnostic_only") and event["details"].get("promotion_eligible")
        )
        live_starts = sum(1 for event in tp_policy_starts if event["details"].get("source_type") == "live_trade")
        live_pairs = len({
            str(event["details"].get("paired_sample_id") or "")
            for event in tp_policy_outcomes
            if event["details"].get("source_type") == "live_trade" and event["details"].get("paired_sample_id")
        })
        fee_rows = []
        for event in [*shadow_starts, *scoped_events]:
            details = event.get("details", {})
            fee = details.get("fee_audit")
            if not fee and isinstance(details.get("codex_v1"), dict):
                fee = details["codex_v1"].get("fee_audit")
            if isinstance(fee, dict):
                fee_rows.append(fee)
        fee_fail = sum(1 for fee in fee_rows if fee.get("fee_buffer_pass") is False)
        status = "PASS" if diagnostic_leaks == 0 else f"diag_leak={diagnostic_leaks}"
        return f"diag_excluded={status}, no_lane_classified={classified_rate:.0f}%, live_tp_pairs={live_pairs}/{live_starts}, fee_audit={len(fee_rows)} rows fail={fee_fail}"

    recent_lines: list[str] = []
    for event in reversed(scoped_events[-10:]):
        details = event["details"]
        lane, status, reason, policy, shadow_lane, candidate_lane = _event_fields(details)
        display_lane = shadow_lane or candidate_lane or lane
        recent_lines.append(
            "  • "
            f"<code>{escape(str(event.get('run_id') or '-'))}</code> "
            f"<code>{escape(str(event.get('event_type') or '-'))}</code> "
            f"lane=<code>{escape(display_lane)}</code> "
            f"status=<code>{escape(status)}</code> "
            f"policy=<code>{escape(policy)}</code> "
            f"reason=<code>{escape(str(reason)[:48])}</code>"
        )

    version_line = _top_pairs(version_counts, limit=3)
    window_label = f"{start_tpe.strftime('%m/%d %H:%M')}→{now_tpe.strftime('%H:%M')} TPE"
    lines = [
        "📊 <b>Codex gate 統計</b>",
        f"  • scope: <code>{escape(scope_label)}</code> / window: <code>{escape(window_label)}</code>",
        f"  • version rows: <code>{version_line}</code>",
        f"  • runtime: <code>{escape(runtime_version)}</code> schema=<code>{escape(schema_version)}</code> config=<code>{escape(config_hash)}</code>",
        f"  • freshness scanner/live/shadow/report: <code>{_latest_time(skip_events + no_lane_candidates)}</code> / <code>{_latest_time([event for event in scoped_events if event.get('event_type') in {'entry_codex_v1_accepted', 'entry_placed', 'entry_filled', 'completed'}])}</code> / <code>{_latest_time(shadow_starts + shadow_outcomes + tp_policy_outcomes)}</code> / <code>{now_tpe.strftime('%H:%M:%S')}</code>",
        f"  • runs=<code>{len(scoped_runs)}</code> traded_runs=<code>{len(traded_runs)}</code> accepted=<code>{accepted_events}</code> placed=<code>{entry_placed_events}</code> filled=<code>{entry_filled_events}</code>",
        f"  • PnL gross=<code>{realized:+.4f}</code> fee=<code>{fees:.4f}</code> net≈<code>{net:+.4f}</code>",
        f"  • run_status: <code>{_top_pairs(run_status_counts, limit=5)}</code>",
        "",
        "🚧 <b>Block 壓力</b>",
        f"  • skipped_rows=<code>{len(skip_events)}</code> affected_runs=<code>{len({str(event.get('run_id') or '-') for event in skip_events})}</code> unique_opps≈<code>{len(shadow_opportunity_ids)}</code>",
        f"  • top_reason rows/runs: <code>{_top_reason_pairs(limit=6)}</code>",
        f"  • top_lane_or_shadow: <code>{_top_pairs(lane_rows, limit=6)}</code>",
        f"  • side/strategy/reason: <code>{_top_pairs(side_reason_rows, limit=4)}</code>",
        f"  • no_lane_bucket: <code>{_top_pairs(no_lane_bucket_counts, limit=6)}</code>",
        "",
        "👻 <b>Shadow outcome</b>",
        f"  • started=<code>{len(shadow_starts)}</code> dropped=<code>{len(shadow_drops)}</code> outcome=<code>{len(shadow_outcomes)}</code> pending≈<code>{pending_shadow}</code>",
        f"  • family: <code>{_top_pairs(shadow_family_counts, limit=6)}</code>",
        f"  • start_by_lane: <code>{_top_pairs(shadow_start_counts, limit=5)}</code>",
        f"  • dropped_by_lane: <code>{_top_pairs(shadow_drop_counts, limit=5)}</code>",
        f"  • outcome_by_lane: <code>{_top_pairs(shadow_outcome_counts, limit=6)}</code>",
        f"  • readiness: <code>{_promotion_readiness(limit=5)}</code>",
        f"  • data_quality: <code>{_data_quality_warnings()}</code>",
        f"  • evidence_integrity: <code>{_evidence_integrity()}</code>",
        "",
        "🎯 <b>TP policy shadow V1.3.2</b>",
        f"  • samples paired=<code>{len(tp_policy_paired_ids)}</code>/50 outcomes=<code>{len(tp_policy_outcomes)}</code> starts=<code>{len(tp_policy_starts)}</code> dropped=<code>{len(tp_policy_drops)}</code> pending≈<code>{tp_policy_pending}</code>",
        f"  • quality: <code>{_tp_policy_quality()}</code>",
        f"  • top_delta: <code>{_tp_policy_summary(limit=3)}</code>",
        "  • live TP override: <code>OFF</code>",
        "",
        "🧾 <b>最近 gate events</b>",
    ]
    lines.extend(recent_lines or ["  • <code>none</code>"])
    return "\n".join(lines)


async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/signal — Show the current Codex live lane map and order principles."""
    if not await _authorized(update, context):
        return
    app_data = context.application.bot_data
    settings: Settings = app_data["settings"]
    logger.info(
        "telegram_cmd_signal_received",
        chat_id=update.effective_chat.id if update.effective_chat else None,
        user_id=update.effective_user.id if update.effective_user else None,
    )

    try:
        from src.gridbot.strategy.codex_v1_live import (
            CODEX_V1_VERSION,
            format_codex_v1_signal_overview,
        )

        codex_live_enabled = bool(
            getattr(settings, "mainnet_codex_v1_enabled", False)
            or getattr(settings, "mainnet_strategy_label", "") in {
                CODEX_V1_VERSION,
                "_codex_v1.4.1",
                "_codex_v1.4.0",
                "_codex_v1.3.3_fee_and_evidence_quality_fix",
                "codex_v1.3.0_w6a_guarded_200cap",
                "_codex_v1.2.12",
                "codex_v1.2.12",
                "_codex_v1.2.11",
                "codex_v1.2.11",
                "_codex_v1.2.10",
                "codex_v1.2.10",
                "_codex_v1.2.9",
                "codex_v1.2.9",
                "_codex_v1.2.8",
                "codex_v1.2.8",
                "_codex_v1.2.7",
                "codex_v1.2.7",
                "_codex_v1.2.6",
                "codex_v1.2.6",
                "_codex_v1.2.5",
                "codex_v1.2.5",
                "_codex_v1.2.1",
                "codex_v1.2.1",
                "_codex_v1.2.0",
                "codex_v1.2.0",
                "_codex_v1.0.1",
                "codex_v1.0.1",
                "_codex_v1.0.0",
                "codex_v1.0.0",
                "codex_v1",
            }
        )
        disabled_lane_names = tuple(
            part.strip()
            for part in str(getattr(settings, "mainnet_codex_v1_disabled_lanes", "") or "").split(",")
            if part.strip()
        )
        report = format_codex_v1_signal_overview(
            execution_wired=codex_live_enabled,
            disabled_lane_names=disabled_lane_names,
            w6_weak_drift_block_enabled=bool(
                getattr(settings, "mainnet_codex_v1_w6_weak_drift_block_enabled", False)
            ),
            w6_weak_drift_threshold_bp=float(
                getattr(settings, "mainnet_codex_v1_w6_weak_drift_threshold_bp", -30.0)
            ),
            w6_deep_pullback_block_enabled=bool(
                getattr(settings, "mainnet_codex_v1_w6_deep_pullback_block_enabled", False)
            ),
            w6_deep_pullback_d30_max_bp=float(
                getattr(settings, "mainnet_codex_v1_w6_deep_pullback_d30_max_bp", -30.0)
            ),
            w6_deep_pullback_adv3_min_bp=float(
                getattr(settings, "mainnet_codex_v1_w6_deep_pullback_adv3_min_bp", 6.5)
            ),
            w6_deep_pullback_rsi_max=float(
                getattr(settings, "mainnet_codex_v1_w6_deep_pullback_rsi_max", 39.0)
            ),
            w6_deep_pullback_vwap_dist_max_bp=float(
                getattr(settings, "mainnet_codex_v1_w6_deep_pullback_vwap_dist_max_bp", -50.0)
            ),
            w6_deep_pullback_pullback_min_bp=float(
                getattr(settings, "mainnet_codex_v1_w6_deep_pullback_pullback_min_bp", 30.0)
            ),
            w2a_tight_block_enabled=bool(
                getattr(settings, "mainnet_codex_v1_w2a_tight_block_enabled", False)
            ),
            w2a_d30_low_bp=float(getattr(settings, "mainnet_codex_v1_w2a_d30_low_bp", -20.0)),
            w2a_d30_high_bp=float(getattr(settings, "mainnet_codex_v1_w2a_d30_high_bp", -5.0)),
            w2a_adv3_low_bp=float(getattr(settings, "mainnet_codex_v1_w2a_adv3_low_bp", 0.0)),
            w2a_adv3_high_bp=float(getattr(settings, "mainnet_codex_v1_w2a_adv3_high_bp", 6.0)),
            w2a_bb_lower_dist_low_bp=float(
                getattr(settings, "mainnet_codex_v1_w2a_bb_lower_dist_low_bp", 5.0)
            ),
            w2a_bb_lower_dist_high_bp=float(
                getattr(settings, "mainnet_codex_v1_w2a_bb_lower_dist_high_bp", 20.0)
            ),
            w1b_tight_block_enabled=bool(
                getattr(settings, "mainnet_codex_v1_w1b_tight_block_enabled", False)
            ),
            w1b_d30_low_bp=float(getattr(settings, "mainnet_codex_v1_w1b_d30_low_bp", -45.0)),
            w1b_d30_high_bp=float(getattr(settings, "mainnet_codex_v1_w1b_d30_high_bp", 5.0)),
            w1b_adv3_high_bp=float(getattr(settings, "mainnet_codex_v1_w1b_adv3_high_bp", 5.0)),
            w1b_bb_lower_dist_high_bp=float(
                getattr(settings, "mainnet_codex_v1_w1b_bb_lower_dist_high_bp", 20.0)
            ),
            w1b_reprice_wait_max_seconds=float(
                getattr(settings, "mainnet_codex_v1_w1b_reprice_wait_max_seconds", 60.0)
            ),
        )
        snapshot = await _build_codex_signal_snapshot(
            context,
            execution_wired=codex_live_enabled,
        )
        if snapshot:
            await _reply_html_chunks(update.message, snapshot)
        stats = await _build_codex_signal_stats(context)
        await _reply_html_chunks(update.message, stats)

    except Exception as exc:
        logger.error("cmd_signal_failed", error=str(exc))
        await update.message.reply_text(f"❌ 取得即時訊號失敗：{str(exc)[:300]}")


async def cmd_mainnet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mainnet — Mainnet one-run status and controls."""
    if not await _authorized(update, context):
        return
    logger.info(
        "telegram_cmd_mainnet_received",
        chat_id=update.effective_chat.id if update.effective_chat else None,
        user_id=update.effective_user.id if update.effective_user else None,
    )
    manager = context.application.bot_data.get("mainnet_one_run_manager")
    if manager is None:
        await update.message.reply_text("❌ Mainnet one-run manager 尚未初始化。")
        return
    status = await manager.status()
    await update.message.reply_text(status.text, parse_mode="HTML", reply_markup=status.reply_markup)


async def cmd_lanes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/lanes — Read-only status for every frozen legacy lane."""
    if not await _authorized(update, context):
        return
    message = update.message
    if message is None:
        return
    db = _lane_monitor_database(context)
    if db is None:
        await message.reply_text("❌ Lane evidence database 尚未初始化。")
        return
    text = await build_lane_monitor(db)
    chunks = lane_monitor_html_chunks(text)
    for index, chunk in enumerate(chunks):
        await message.reply_text(
            chunk,
            parse_mode="HTML",
            reply_markup=lane_monitor_keyboard() if index == len(chunks) - 1 else None,
        )


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    message = update.message
    if message is None or not message.text:
        return
    text = message.text.strip()
    logger.info(
        "telegram_text_received",
        chat_id=update.effective_chat.id if update.effective_chat else None,
        user_id=update.effective_user.id if update.effective_user else None,
        text_preview=text[:40],
    )
    if "已下單" not in text and "已開單" not in text:
        return
    await _confirm_manual_signal(
        update=update,
        context=context,
        source="telegram_text",
        replied_message_id=message.reply_to_message.message_id if message.reply_to_message else None,
        raw_text=text,
    )


async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    if not update.message:
        return
    await update.message.reply_text(
        "這個舊功能目前已停用。\n\n"
        "現在這支 bot 的主要流程是：\n"
        "/signal 查看即時判斷\n"
        "/pnl 查看 mainnet 今日 PnL\n"
        "/mainnet 啟動/查詢 mainnet one-run\n"
        "/pause 暫停訊號推送\n"
        "/resume 恢復訊號推送\n\n"
        "收到開單通知後，手動下單並按訊息下方的「已下單」。"
    )


async def handle_manual_signal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    query = update.callback_query
    if query is None:
        return
    logger.info(
        "telegram_manual_signal_callback_received",
        chat_id=update.effective_chat.id if update.effective_chat else None,
        user_id=update.effective_user.id if update.effective_user else None,
        data=query.data,
        message_id=query.message.message_id if query.message else None,
    )
    await query.answer("已記錄，正在抓 mainnet 摘要...")
    data = query.data or ""
    replied_message_id = query.message.message_id if query.message else None
    await _confirm_manual_signal(
        update=update,
        context=context,
        source="telegram_button",
        replied_message_id=replied_message_id,
        raw_text=data,
    )


async def handle_mainnet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""
    lane_monitor_request = data in {"mainnet:lanes", "mainnet:lanes:refresh"} or data.startswith(
        "mainnet:lane:"
    )
    manager = context.application.bot_data.get("mainnet_one_run_manager")
    if manager is None and not lane_monitor_request:
        await query.answer("Mainnet manager 尚未初始化。", show_alert=True)
        return
    await query.answer("處理中...")
    message = query.message
    if message is None:
        return
    if data in {"mainnet:lanes", "mainnet:lanes:refresh"}:
        db = _lane_monitor_database(context)
        if db is None:
            await message.reply_text("❌ Lane evidence database 尚未初始化。")
            return
        text = await build_lane_monitor(db)
        chunks = lane_monitor_html_chunks(text)
        for index, chunk in enumerate(chunks):
            await message.reply_text(
                chunk,
                parse_mode="HTML",
                reply_markup=lane_monitor_keyboard() if index == len(chunks) - 1 else None,
            )
        return
    if data.startswith("mainnet:lane:"):
        db = _lane_monitor_database(context)
        if db is None:
            await message.reply_text("❌ Lane evidence database 尚未初始化。")
            return
        lane_code = data.removeprefix("mainnet:lane:")
        text = await build_lane_detail(db, lane_code)
        chunks = lane_monitor_html_chunks(text)
        for index, chunk in enumerate(chunks):
            await message.reply_text(
                chunk,
                parse_mode="HTML",
                reply_markup=lane_monitor_keyboard() if index == len(chunks) - 1 else None,
            )
        return
    if data == "mainnet:adaptive:start":
        result = await manager.start_adaptive_session(actor="telegram")
        text = result if isinstance(result, str) else result.text
        reply_markup = None if isinstance(result, str) else getattr(result, "reply_markup", None)
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return
    if data == "mainnet:adaptive:status":
        result = await manager.adaptive_status()
        text = result if isinstance(result, str) else result.text
        reply_markup = None if isinstance(result, str) else getattr(result, "reply_markup", None)
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return
    if data == "mainnet:adaptive:review":
        result = await manager.adaptive_review()
        text = result if isinstance(result, str) else result.text
        reply_markup = None if isinstance(result, str) else getattr(result, "reply_markup", None)
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return
    if data == "mainnet:cancel":
        text = await manager.cancel()
        await query.message.reply_text(text, parse_mode="HTML")
        return
    if data == "mainnet:stop_loop":
        text = await manager.stop_loop()
        await query.message.reply_text(text, parse_mode="HTML")
        return
    if data.startswith("mainnet:arm"):
        # mainnet:arm (legacy) = 1 run; mainnet:arm:N = N runs
        loop_count = 1
        if data.startswith("mainnet:arm:"):
            try:
                loop_count = int(data.split(":")[-1])
            except ValueError:
                loop_count = 1
        if loop_count < 1:
            loop_count = 1
        text = await manager.arm(actor="telegram", loop_count=loop_count)
        await query.message.reply_text(text, parse_mode="HTML")
        return
    if data.startswith("mainnet:notional:"):
        # User-adjustable ticket size (200/300/500/1000 USDC).
        try:
            value = float(data.split(":")[-1])
        except ValueError:
            await query.message.reply_text("❌ 金額格式錯誤。")
            return
        text = await manager.set_notional(value)
        await query.message.reply_text(text, parse_mode="HTML")
        return
    if data.startswith("mainnet:losscap:"):
        # Loop cumulative-loss protection cap (0 = off).
        try:
            value = float(data.split(":")[-1])
        except ValueError:
            await query.message.reply_text("❌ 保護門檻格式錯誤。")
            return
        text = await manager.set_loop_loss_cap(value)
        await query.message.reply_text(text, parse_mode="HTML")
        return
    status = await manager.status()
    await query.message.reply_text(status.text, parse_mode="HTML", reply_markup=status.reply_markup)


async def _confirm_manual_signal(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    source: str,
    replied_message_id: int | None,
    raw_text: str,
) -> None:
    trader = context.application.bot_data.get("trader")
    if trader is None:
        if update.effective_message:
            await update.effective_message.reply_text("❌ 目前沒有可對應的即時訊號來源。")
        return

    signal_info = None
    if replied_message_id is not None:
        signal_info = trader.get_manual_signal_message(replied_message_id)
    if signal_info is None:
        signal_info = trader.get_latest_manual_signal()
    if signal_info is None:
        signal_info = await _latest_manual_signal_from_audit(context)
        if signal_info is not None:
            logger.info(
                "telegram_manual_signal_recovered_from_audit",
                execution_id=signal_info.get("execution_id"),
                message_id=signal_info.get("message_id"),
                requested_message_id=replied_message_id,
            )
    if signal_info is None:
        if update.effective_message:
            await update.effective_message.reply_text("❌ 找不到可配對的訊號，請直接回覆那則訊號訊息或按該訊息下方按鈕。")
        return

    settings: Settings = context.application.bot_data["settings"]
    audit_repo: AuditLogRepository = context.application.bot_data["audit_repo"]
    mainnet_snapshot = await _fetch_mainnet_trade_snapshot(settings, signal_info)
    details = {
        "telegram_message_id": update.effective_message.message_id if update.effective_message else None,
        "telegram_reply_to_message_id": replied_message_id,
        "telegram_text": raw_text,
        "source": source,
        "confirmed_at_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
        "signal": signal_info,
        "mainnet_snapshot": mainnet_snapshot,
    }
    await audit_repo.log("manual_signal_confirmed", "telegram_user", details)

    if update.effective_message:
        await update.effective_message.reply_text(
            "✅ 已記錄這次人工下單確認\n"
            f"交易對：<code>{escape(str(signal_info.get('symbol') or ''))}</code>\n"
            f"方向：<b>{escape(str(signal_info.get('direction') or ''))}</b>\n"
            f"策略：<b>{escape(str(signal_info.get('strategy') or ''))}</b>\n"
            f"訊號代碼：<code>{escape(str(signal_info.get('execution_id') or ''))}</code>\n"
            f"{_format_mainnet_snapshot(mainnet_snapshot)}",
            parse_mode="HTML",
        )
