"""Set the Telegram command menu for the Cry3 testnet bot."""

from __future__ import annotations

import asyncio
import os

from telegram import Bot, BotCommand


async def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    bot = Bot(token)
    ok = await bot.set_my_commands(
        [
            BotCommand("testnet", "testnet status"),
            BotCommand("pause", "pause scheduler"),
            BotCommand("resume", "resume scheduler"),
            BotCommand("help", "show commands"),
        ]
    )
    print(f"set_commands_ok={ok}")


if __name__ == "__main__":
    asyncio.run(main())
