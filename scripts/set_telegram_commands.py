"""Set the Telegram command menu for both Cry3 mainnet and testnet bots."""

from __future__ import annotations

import asyncio
from telegram import Bot, BotCommand

# Both tokens from .env and testnet/.env.testnet
TOKENS = [
    "8717329877:AAF-IMYCHxI866ixaP4Vgb8AnbZiEPji8Ko",  # Mainnet bot
    "8830813930:AAGLNGyQjEp_66bIxRu2y2zcAjBxUTyE6uQ",  # Testnet bot
]

COMMANDS = [
    BotCommand("signal", "即時策略訊號與子邏輯診斷"),
    BotCommand("mainnet", "手動實盤 one-run 驗證與開單"),
    BotCommand("testnet", "查看 Testnet 當前部位狀態"),
    BotCommand("pnl", "查看今日/昨日交易損益"),
    BotCommand("ai", "使用 Gemini 分析最新交易市況"),
    BotCommand("pause", "暫停 Testnet 自動化調度器"),
    BotCommand("resume", "恢復 Testnet 自動化調度器"),
    BotCommand("help", "顯示所有功能命令清單"),
]

async def main() -> None:
    for token in TOKENS:
        try:
            bot = Bot(token)
            me = await bot.get_me()
            print(f"Setting commands for bot: @{me.username} ({me.id})")
            ok = await bot.set_my_commands(COMMANDS)
            print(f"  Result: {ok}")
        except Exception as e:
            print(f"  Failed for token {token[:15]}...: {e}")

if __name__ == "__main__":
    asyncio.run(main())
