from __future__ import annotations

import asyncio
import os
from getpass import getpass

from telethon import TelegramClient
from telethon.sessions import StringSession


try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None


async def main() -> None:
    if load_dotenv:
        load_dotenv()

    api_id = int(os.getenv("TELEGRAM_API_ID") or input("TELEGRAM_API_ID: ").strip())
    api_hash = os.getenv("TELEGRAM_API_HASH") or input("TELEGRAM_API_HASH: ").strip()
    phone = os.getenv("TELEGRAM_PHONE") or input("Telefono Telegram (+39...): ").strip()

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start(phone=phone, password=lambda: getpass("Password 2FA: "))

    print()
    print("TELEGRAM_SESSION=" + client.session.save())
    print()
    print("Set this value as a secret in your host. Do not commit it.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
