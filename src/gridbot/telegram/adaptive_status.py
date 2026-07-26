"""Concise adaptive-first Telegram status view backed by the runtime ledger."""
from __future__ import annotations

import json
from collections.abc import Mapping
from html import escape

from telegram import Update
from telegram.ext import ContextTypes

from src.gridbot.telegram.handlers import _authorized


_ACTIVE_STATUSES = {"ARMED", "ENTRY_PENDING", "RUNNING", "CLOSING"}


def _json_object(value: object) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _number(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _adaptive_metadata(row: Mapping) -> dict:
    params = _json_object(row.get("params_json"))
    adaptive = params.get("adaptive")
    return dict(adaptive) if isinstance(adaptive, Mapping) else {}


def _signal_summary(row: Mapping) -> tuple[str, str, str, str, str]:
    signal = _json_object(row.get("signal_json"))
    codex = signal.get("codex_v1")
    codex = dict(codex) if isinstance(codex, Mapping) else {}
    effective = codex.get("effective_execution")
    effective = dict(effective) if isinstance(effective, Mapping) else {}
    metrics = codex.get("metrics")
    metrics = dict(metrics) if isinstance(metrics, Mapping) else {}
    adaptive = signal.get("adaptive")
    adaptive = dict(adaptive) if isinstance(adaptive, Mapping) else {}
    decision = adaptive.get("decision")
    decision = dict(decision) if isinstance(decision, Mapping) else {}

    version = str(codex.get("version") or signal.get("version") or "-")
    lane = str(codex.get("lane_code") or signal.get("lane_code") or decision.get("lane_code") or "-")
    state = str(codex.get("market_state") or signal.get("market_state") or decision.get("market_state") or "-")
    route = str(effective.get("route") or codex.get("effective_route") or decision.get("route") or "-")
    action = str(
        signal.get("action")
        or codex.get("action")
        or metrics.get("live_action")
        or decision.get("action")
        or "-"
    )
    return version, lane, state, route, action


async def build_adaptive_status(db) -> str:
    rows = await db.fetchall(
        """
        SELECT run_id, status, signal_json, params_json, realized_pnl_usdc,
               commission_usdc, exit_reason, armed_at_ms
        FROM mainnet_runs
        WHERE params_json LIKE '%adaptive_continuous%'
        ORDER BY armed_at_ms ASC
        """
    )
    if not rows:
        return "♾ Adaptive session 尚無資料。"

    metadata = [_adaptive_metadata(row) for row in rows]
    session_id = ""
    for row, item in reversed(list(zip(rows, metadata))):
        if row.get("status") in _ACTIVE_STATUSES:
            session_id = str(item.get("session_id") or "")
            if session_id:
                break
    if not session_id:
        session_id = str(metadata[-1].get("session_id") or "")

    session_rows = [
        row
        for row, item in zip(rows, metadata)
        if not session_id or str(item.get("session_id") or "") == session_id
    ]
    if not session_rows:
        session_rows = [rows[-1]]

    active = next((row for row in reversed(session_rows) if row.get("status") in _ACTIVE_STATUSES), None)
    current = active or session_rows[-1]
    version, lane, state, route, action = _signal_summary(current)
    terminal_rows = [row for row in session_rows if row.get("status") not in _ACTIVE_STATUSES]
    net_pnl = sum(
        _number(row.get("realized_pnl_usdc")) - abs(_number(row.get("commission_usdc")))
        for row in terminal_rows
    )
    stop_reason = str(current.get("exit_reason") or "等待 entry/gate")
    status = str(current.get("status") or "-")
    run_id = str(current.get("run_id") or "-")

    return (
        "♾ <b>Adaptive status</b>\n"
        f"版本：<code>{escape(version)}</code>\n"
        f"Session：<code>{escape(session_id or '-')}</code>\n"
        f"目前：<b>第 {len(session_rows)} run</b> | <code>{escape(run_id)}</code>\n"
        f"狀態：<b>{escape(status)}</b>\n"
        f"Lane/state：<code>{escape(lane)}</code> / <code>{escape(state)}</code>\n"
        f"Route/action：<code>{escape(route)}</code> / <code>{escape(action)}</code>\n"
        f"完成：<b>{len(terminal_rows)}</b> | session 淨損益：<b>{net_pnl:+.4f} USDC</b>\n"
        f"Exit/gate：<code>{escape(stop_reason)}</code>\n"
        "風險：$50 / cap −$2 / giveback $1 / DCA off\n"
        "舊 Testnet：<b>OFF</b>"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — Show the concise adaptive status from the runtime ledger."""
    if not await _authorized(update, context):
        return
    message = update.effective_message
    if message is None:
        return
    db = context.application.bot_data.get("db")
    if db is None:
        await message.reply_text("❌ Runtime DB 尚未初始化。")
        return
    try:
        await message.reply_text(await build_adaptive_status(db), parse_mode="HTML")
    except Exception as exc:
        await message.reply_text(f"❌ 取得 Adaptive 狀態失敗：{escape(str(exc)[:300])}")

