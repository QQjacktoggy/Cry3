"""Minimal user-facing Telegram commands for the active mainnet workflow."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from src.gridbot.telegram.handlers import _authorized


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    await update.effective_message.reply_text(
        "🟢 <b>Cry3 Mainnet Adaptive</b>\n\n"
        "目前以 ETHUSDC mainnet adaptive one-run 為主。\n"
        "使用 /status 查看第幾 run、狀態、lane/action 與 session PnL；"
        "使用 /mainnet 管理 run；/lanes 查看全部 legacy lane；"
        "/pnl 查看今日結果。\n"
        "舊 Testnet 訊號通知已停用。",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update, context):
        return
    await update.effective_message.reply_text(
        "📋 <b>目前 Telegram 功能</b>\n\n"
        "➡️ /status — Adaptive 第幾 run、狀態、lane/action 與 session PnL\n"
        "➡️ /lanes — 查看 27 條 legacy lane 的 Live/Shadow 與 evidence 狀態\n"
        "➡️ /mainnet — 啟動、查詢或停止 mainnet Adaptive run\n"
        "➡️ /pnl — 查看 mainnet 今日 PnL 與成交分析\n"
        "➡️ /help — 顯示此說明\n\n"
        "舊 Testnet trader、signal 通知與 daily report 已停用。",
        parse_mode="HTML",
    )
