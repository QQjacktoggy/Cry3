"""Telegram bot command handlers for Testnet Live Auto Trader.

Each handler corresponds to a / command defined in spec.
All handlers receive ApplicationContext from python-telegram-bot v21.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import ContextTypes

from config.settings import Settings
from src.gridbot.ai.gemini import GeminiAnalyzer
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.binance.models import FuturesTrade, IncomeRecord, PositionInfo
from src.gridbot.storage.repositories import AuditLogRepository
from src.gridbot.telegram.formatters import (
    format_testnet_dashboard,
)
from src.gridbot.testnet.pnl import calculate_testnet_pnl_breakdown
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)
TAIPEI = ZoneInfo("Asia/Taipei")


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
    binance_client: BinanceFuturesClient = app_data["binance_client"]
    settings: Settings = app_data["settings"]

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


async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/signal — Confirm current signal status in real-time."""
    if not await _authorized(update, context):
        return
    app_data = context.application.bot_data
    settings: Settings = app_data["settings"]
    trader = app_data.get("trader")
    logger.info(
        "telegram_cmd_signal_received",
        chat_id=update.effective_chat.id if update.effective_chat else None,
        user_id=update.effective_user.id if update.effective_user else None,
    )

    if not trader:
        await update.message.reply_text("❌ 自動交易模組尚未完全初始化或未在實盤模式下啟動。")
        return

    await update.message.reply_text("⏳ 正在讀取最新 K 線與策略指標，計算即時訊號中...")

    try:
        from datetime import datetime
        from src.gridbot.testnet.auto_trader import (
            SIDE_LABELS,
            REGIME_LABELS,
            RISK_MODE_LABELS,
            PLAYBOOK_LABELS,
            ALLOCATOR_PROFILE_LABELS,
            ALLOCATOR_STATE_LABELS,
        )
        from src.gridbot.strategy.winrate_optimized_portfolio import (
            describe_winrate_optimized_portfolio_status,
        )

        now_str = datetime.now(TAIPEI).strftime("%Y/%m/%d %H:%M:%S")
        lines = [f"📡 <b>即時策略訊號狀態報告</b>", f"發送時間: <code>{now_str}</code>\n"]

        for symbol in settings.symbols_list:
            candles = await trader._load_candles(symbol)
            if not candles:
                lines.append(f"━━ <b>{symbol} 評估報告</b> ━━\n❌ 無法獲取最新的 K 線數據\n")
                continue

            today_net = 0.0 if settings.testnet_telegram_signal_only else await trader._today_net_pnl(symbol)
            decision = trader._live_signal_decision(symbol, candles, today_net)
            sig = decision.signal

            action = sig.action
            action_emoji = "🟢" if action in ("BUY", "LONG", "PLAN_LONG") else "🔴" if action in ("SELL", "SHORT", "PLAN_SHORT") else "➡️"
            action_lbl = SIDE_LABELS.get(action, action)
            is_no_signal_wait = (
                action == "WAIT"
                and decision.strategy in ("portfolio_wait", "wildcat_wait")
            )

            strategy_name = settings.testnet_strategy_label

            lines.append(f"━━ <b>{symbol} 評估報告</b> ━━")
            lines.append(f"策略核心: <code>{strategy_name}</code>")
            lines.append(f"最新價格: <b>${sig.price:.4f} USDC</b>")
            if is_no_signal_wait:
                lines.append("🎯 <b>訊號決策: ➡️ WAIT（目前無新訊號）</b>")
            else:
                lines.append(f"🎯 <b>訊號決策: {action_emoji} {action_lbl}</b>")
            lines.append(f"得分: <code>{sig.score}</code> | 信心度: <code>{sig.confidence}%</code>")

            # Indicators
            rsi_val = f"{sig.rsi:.2f}" if sig.rsi is not None else "N/A"
            atr_val = f"{sig.atr:.4f}" if sig.atr is not None else "N/A"
            vwap_val = f"${sig.vwap:.4f}" if sig.vwap is not None else "N/A"
            sup_val = f"${sig.support:.4f}" if sig.support is not None else "N/A"
            if is_no_signal_wait:
                lines.append("📊 指標摘要: <code>本輪未形成可執行 setup，因此不展開 RSI / ATR / VWAP 細節</code>")
            else:
                lines.append(f"📊 RSI: <code>{rsi_val}</code> | ATR: <code>{atr_val}</code>")
                lines.append(f"🧱 VWAP: <code>{vwap_val}</code> | 支撐: <code>{sup_val}</code>")

            # Allocator and Regime states
            regime_lbl = REGIME_LABELS.get(decision.regime, decision.regime)
            risk_lbl = RISK_MODE_LABELS.get(decision.risk_mode, decision.risk_mode)
            playbook_lbl = PLAYBOOK_LABELS.get(decision.market_playbook, decision.market_playbook)
            state_lbl = ALLOCATOR_STATE_LABELS.get(decision.allocator_state, decision.allocator_state)
            profile_lbl = ALLOCATOR_PROFILE_LABELS.get(decision.allocator_profile, decision.allocator_profile)

            lines.append("")
            lines.append("⚙️ <b>配置器與市場狀態:</b>")
            lines.append(f"  • 市場型態: <code>{regime_lbl}</code>")
            lines.append(f"  • 風控模式: <code>{risk_lbl}</code>")
            lines.append(f"  • 交易劇本: <code>{playbook_lbl}</code>")
            lines.append(f"  • 分配倍率: <code>{decision.allocator_scale:.2f}x</code>")
            lines.append(f"  • 分配狀態: <code>{state_lbl}</code> | 配置: <code>{profile_lbl}</code>")
            if is_no_signal_wait:
                lines.append("  • 解讀: <code>策略正常待機，這一刻沒有通過條件的進場訊號</code>")

            # Planned parameters if signal is buy/sell
            if action in ("BUY", "SELL", "LONG", "SHORT") and sig.planned_qty > 0:
                lines.append("")
                lines.append("📐 <b>訊號開倉計畫參數:</b>")
                lines.append(f"  • 進場價: ${sig.price:.4f}")
                lines.append(f"  • 計畫數量: {sig.planned_qty:.6f}")
                lines.append(f"  • 計畫名目價值: ${sig.planned_notional_usdc:.2f} USDC")
                lines.append(f"  • 估計保證金: ${sig.planned_margin_usdc:.4f} USDC")
                if sig.stop_loss:
                    lines.append(f"  • 建議止損: <b>${sig.stop_loss:.4f}</b>")
                if sig.take_profits:
                    tp_strs = ", ".join(f"${tp:.4f}" for tp in sig.take_profits)
                    lines.append(f"  • 建議止盈: <b>{tp_strs}</b>")

            # Reasons
            lines.append("")
            lines.append("💡 <b>決策理由 / 條件過濾細節:</b>")
            if sig.reasons:
                for r in sig.reasons:
                    lines.append(f"  • {escape(r)}")
            else:
                lines.append("  • 無特定描述")

            if settings.testnet_strategy_label == "winrate_optimized_portfolio":
                status = describe_winrate_optimized_portfolio_status(
                    candles=candles,
                    today_net=today_net,
                    cooldown_until=getattr(trader, "_cooldown_until", {}),
                    equity_usdc=float(getattr(settings, "testnet_equity_usdc", 150.0)),
                )
                lines.append("")
                lines.append("🧭 <b>S1~S5 即時狀態列表</b>")
                summary = status.get("summary") or {}
                if status.get("ready"):
                    lines.append(
                        "市況："
                        f"<code>trend={escape(str(summary.get('trend')))}</code> / "
                        f"<code>vol={escape(str(summary.get('vol')))}</code> / "
                        f"<code>vol_ratio={float(summary.get('vol_ratio') or 0):.2f}</code> / "
                        f"<code>body={float(summary.get('body_ratio') or 0):.2f}</code>"
                    )
                    ready_candidates = status.get("ready_candidates") or []
                    if ready_candidates:
                        lines.append("✅ <b>目前通過候選：</b>")
                        for row in ready_candidates:
                            direction = str(row.get("direction") or "WAIT")
                            side_text = "做多" if direction == "LONG" else "做空" if direction == "SHORT" else "等待"
                            lines.append(
                                f"  • <b>{side_text}</b> | "
                                f"{escape(str(row.get('name') or row.get('key')))} | "
                                f"score=<code>{int(row.get('score') or 0)}</code>"
                            )
                    else:
                        lines.append("ℹ️ <b>目前沒有任何 S1~S5 通過開單門檻；下方列出每個策略卡點。</b>")
                    status_icon = {
                        "ready": "✅",
                        "watch": "👀",
                        "inactive": "⏸",
                        "cooldown": "🧊",
                    }
                    status_label = {
                        "ready": "可開",
                        "watch": "監控",
                        "inactive": "不適用",
                        "cooldown": "冷卻",
                    }
                    direction_label = {
                        "LONG": "做多",
                        "SHORT": "做空",
                        "WAIT": "等待",
                    }
                    for row in status.get("strategies") or []:
                        row_status = str(row.get("status") or "inactive")
                        direction = str(row.get("direction") or "WAIT")
                        lines.append(
                            f"{status_icon.get(row_status, '•')} "
                            f"<b>{escape(str(row.get('name') or row.get('key')))}</b> "
                            f"<code>{status_label.get(row_status, row_status)}</code> | "
                            f"{direction_label.get(direction, direction)} | "
                            f"score=<code>{int(row.get('score') or 0)}</code> "
                            f"/ level=<code>{int(row.get('level_score') or 0)}</code>"
                        )
                        lines.append(f"   └ {escape(str(row.get('reason') or ''))}")
                else:
                    lines.append(f"  • {escape(str(summary.get('reason') or 'S1~S5 狀態尚未就緒'))}")
            lines.append("")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as exc:
        logger.error("cmd_signal_failed", error=str(exc))
        await update.message.reply_text(f"❌ 取得即時訊號失敗：{str(exc)[:300]}")


async def cmd_mainnet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mainnet — Mainnet one-run status and controls."""
    if not await _authorized(update, context):
        return
    manager = context.application.bot_data.get("mainnet_one_run_manager")
    if manager is None:
        await update.message.reply_text("❌ Mainnet one-run manager 尚未初始化。")
        return
    status = await manager.status()
    await update.message.reply_text(status.text, parse_mode="HTML", reply_markup=status.reply_markup)


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
    manager = context.application.bot_data.get("mainnet_one_run_manager")
    if manager is None:
        await query.answer("Mainnet manager 尚未初始化。", show_alert=True)
        return
    data = query.data or ""
    await query.answer("處理中...")
    if data == "mainnet:arm":
        text = await manager.arm(actor="telegram")
        await query.message.reply_text(text, parse_mode="HTML")
        return
    if data == "mainnet:cancel":
        text = await manager.cancel()
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
