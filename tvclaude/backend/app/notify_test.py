"""Send one test message to Telegram and Discord using the values in .env.

    python -m app.notify_test

Prints ok / failed / not set per channel. Never prints the webhook URL or bot token.
"""
import asyncio
import sys

from .config import get_settings
from .infra import Discord, Telegram

MSG = "tvclaude test: notifications are connected. (paper trading, nothing was sent to a broker)"


async def main() -> int:
    s = get_settings()
    channels = {"telegram": Telegram(s.telegram_bot_token, s.telegram_chat_id),
                "discord": Discord(s.discord_webhook_url)}
    worked = 0
    for name, ch in channels.items():
        if not ch.enabled:
            print(f"{name}: not set in .env")
            continue
        ok = await ch.send(MSG)
        worked += ok
        print(f"{name}: {'ok, check the channel' if ok else 'failed (check the value in .env; see the log line above)'}")
    return 0 if worked else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
