from __future__ import annotations

import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None


async def main() -> None:
    if load_dotenv:
        load_dotenv()

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session = os.environ["TELEGRAM_SESSION"]

    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.start()
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        username = getattr(entity, "username", None)
        username_display = f"@{username}" if username else ""
        print(f"{dialog.id}\t{username_display}\t{dialog.name}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
