from __future__ import annotations

import asyncio
import re
from getpass import getpass
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import ApiIdInvalidError, PhoneNumberInvalidError
from telethon.sessions import StringSession


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"


def load_existing_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def ask(name: str, prompt: str, existing: dict[str, str], secret: bool = False) -> str:
    current = existing.get(name, "")
    suffix = " [gia' presente, premi Invio per tenerlo]" if current else ""
    reader = getpass if secret else input
    value = reader(f"{prompt}{suffix}: ").strip()
    return current if not value and current else value


def ask_required(
    name: str,
    prompt: str,
    existing: dict[str, str],
    secret: bool = False,
) -> str:
    while True:
        value = ask(name, prompt, existing, secret=secret)
        if value:
            return value
        print(f"{name} non puo' essere vuoto.")


def normalize_phone(value: str) -> str:
    cleaned = value.replace(" ", "").strip()
    if cleaned and not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


def ask_api_hash(existing: dict[str, str]) -> str:
    while True:
        value = ask_required(
            "TELEGRAM_API_HASH",
            "TELEGRAM_API_HASH",
            existing,
            secret=True,
        )
        if re.fullmatch(r"[0-9a-fA-F]{32}", value):
            print(
                "Hash letto correttamente: "
                f"{len(value)} caratteri, inizia con {value[:4]}, finisce con {value[-4:]}"
            )
            return value.lower()
        print(
            "TELEGRAM_API_HASH deve avere 32 caratteri esadecimali. "
            "Se Ctrl+V non funziona nella riga nascosta, usa tasto destro > Incolla."
        )


async def create_session(api_id: int, api_hash: str) -> str:
    phone = normalize_phone(
        input("Numero Telegram dell'account che legge le offerte (+39...): ")
    )
    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.start(
            phone=phone,
            password=lambda: getpass("Password 2FA, se presente: "),
        )
        session = client.session.save()
    finally:
        await client.disconnect()
    return session


def write_env(values: dict[str, str]) -> None:
    lines = [
        "TELEGRAM_API_ID=" + values["TELEGRAM_API_ID"],
        "TELEGRAM_API_HASH=" + values["TELEGRAM_API_HASH"],
        "TELEGRAM_SESSION=" + values["TELEGRAM_SESSION"],
        "",
        "SOURCE_CHATS=" + values["SOURCE_CHATS"],
        "DESTINATION_CHAT=" + values["DESTINATION_CHAT"],
        "TELEGRAM_BOT_TOKEN=" + values.get("TELEGRAM_BOT_TOKEN", ""),
        "ADMIN_USER_IDS=",
        "",
        "DATABASE_PATH=data/shopperbot.sqlite3",
        "COPY_MEDIA=true",
        "INCLUDE_SOURCE_LINK=true",
        "JOIN_SOURCES=false",
        "MONITOR_ALL_CHATS=false",
        "STARTUP_BACKFILL_LIMIT=0",
        "DEDUPE_TTL_DAYS=14",
        "MIN_POST_INTERVAL_SECONDS=1",
        "MAX_TEXT_CHARS=3500",
        "ALLOW_PATTERNS=",
        "SKIP_PATTERNS=",
        "LOG_LEVEL=INFO",
        "DRY_RUN=false",
        "",
    ]
    ENV_PATH.write_text("\n".join(lines), encoding="utf-8")


async def main() -> None:
    existing = load_existing_env()

    print("1) Apri https://my.telegram.org")
    print("2) Login con il tuo numero Telegram")
    print("3) Vai in API development tools e crea una app qualunque")
    print("4) Copia api_id e api_hash qui sotto")
    print()

    api_id_text = ask_required("TELEGRAM_API_ID", "TELEGRAM_API_ID", existing)
    api_hash = ask_api_hash(existing)
    session = existing.get("TELEGRAM_SESSION", "")
    try:
        api_id = int(api_id_text)
    except ValueError:
        raise SystemExit("TELEGRAM_API_ID deve essere un numero intero.")

    try:
        if not session:
            session = await create_session(api_id, api_hash)
        else:
            keep = input("TELEGRAM_SESSION gia' presente. Tenerla? [S/n]: ").strip().lower()
            if keep != "n":
                session = existing["TELEGRAM_SESSION"]
            else:
                session = await create_session(api_id, api_hash)
    except ApiIdInvalidError as exc:
        raise SystemExit(
            "Telegram ha rifiutato api_id/api_hash: ricopia entrambi dalla stessa "
            "pagina su https://my.telegram.org/apps e riprova."
        ) from exc
    except PhoneNumberInvalidError as exc:
        raise SystemExit(
            "Numero Telegram non valido. Inseriscilo in formato internazionale, "
            "per esempio +39..."
        ) from exc

    print()
    print("SOURCE_CHATS puo' contenere @username o ID, separati da virgola.")
    print("Dopo questo setup puoi usare: python scripts\\list_chats.py")
    print("per vedere gli ID precisi delle chat disponibili.")
    print()

    values = {
        "TELEGRAM_API_ID": api_id_text,
        "TELEGRAM_API_HASH": api_hash,
        "TELEGRAM_SESSION": session,
        "SOURCE_CHATS": ask(
            "SOURCE_CHATS",
            "SOURCE_CHATS (puoi lasciarlo vuoto e compilarlo dopo list_chats.py)",
            existing,
        ),
        "DESTINATION_CHAT": ask(
            "DESTINATION_CHAT",
            "DESTINATION_CHAT (puoi lasciarlo vuoto e compilarlo dopo list_chats.py)",
            existing,
        ),
        "TELEGRAM_BOT_TOKEN": ask(
            "TELEGRAM_BOT_TOKEN",
            "Nuovo TELEGRAM_BOT_TOKEN (opzionale, consigliato se vuoi pubblicare come bot)",
            existing,
            secret=True,
        ),
    }
    write_env(values)
    print()
    print(f".env scritto in: {ENV_PATH}")
    print("Prossimo controllo: python scripts\\list_chats.py")


if __name__ == "__main__":
    asyncio.run(main())
