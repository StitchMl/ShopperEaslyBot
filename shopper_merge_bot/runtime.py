from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from telethon import TelegramClient, events, utils
from telethon.errors import AccessTokenInvalidError, ApiIdInvalidError, FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import Message

from .chat_folder import import_chat_folder
from .config import ConfigError, Settings, parse_chat_ref
from .dedupe import DedupeStore
from .formatter import build_outbound_text, trim_text
from .normalization import build_fingerprint


LOGGER = logging.getLogger("shopper_merge_bot")
CAPTION_LIMIT = 1024


@dataclass
class RuntimeState:
    source_ids: set[str] = field(default_factory=set)
    destination_ref: str | None = None
    destination: object | None = None
    ignored_chat_ids: set[str] = field(default_factory=set)


class RateLimiter:
    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self._next_allowed_at = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next_allowed_at:
                await asyncio.sleep(self._next_allowed_at - now)
            self._next_allowed_at = time.monotonic() + self.min_interval_seconds


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logging.getLogger("telethon.network.mtprotosender").setLevel(logging.WARNING)


def invite_hash(ref: str | int) -> str | None:
    if not isinstance(ref, str):
        return None
    cleaned = ref.strip()
    markers = ("t.me/+", "telegram.me/+", "joinchat/")
    for marker in markers:
        if marker in cleaned:
            return cleaned.split(marker, 1)[1].split("?", 1)[0].strip("/")
    return None


async def maybe_join_source(client: TelegramClient, ref: str | int) -> None:
    hash_value = invite_hash(ref)
    try:
        if hash_value:
            await client(ImportChatInviteRequest(hash_value))
        elif isinstance(ref, str):
            await client(JoinChannelRequest(ref))
    except Exception as exc:
        LOGGER.info("Could not join source %s automatically: %s", ref, exc)


async def resolve_sources(
    client: TelegramClient,
    refs: Iterable[str | int],
    join_sources: bool,
) -> list[object]:
    sources: list[object] = []
    for ref in refs:
        if join_sources:
            await maybe_join_source(client, ref)
        sources.append(await client.get_entity(ref))
    return sources


def entity_title(entity: object) -> str:
    return (
        getattr(entity, "title", None)
        or " ".join(
            part
            for part in (
                getattr(entity, "first_name", None),
                getattr(entity, "last_name", None),
            )
            if part
        )
        or getattr(entity, "username", None)
        or str(utils.get_peer_id(entity))
    )


def save_source_entity(store: DedupeStore, entity: object) -> None:
    store.add_source(
        peer_id=str(utils.get_peer_id(entity)),
        title=str(entity_title(entity)),
        username=str(getattr(entity, "username", None) or ""),
    )


async def resolve_saved_sources(
    client: TelegramClient,
    store: DedupeStore,
) -> list[object]:
    sources = []
    for source in store.list_sources():
        try:
            sources.append(await client.get_entity(parse_chat_ref(source.peer_id)))
        except Exception as exc:
            LOGGER.warning("Could not resolve saved source %s: %s", source.peer_id, exc)
    return sources


async def resolve_dialog_ref(client: TelegramClient, ref: str) -> object:
    normalized = ref.strip()
    parsed = parse_chat_ref(normalized)
    target_id = str(parsed) if isinstance(parsed, int) else None
    target_username = normalized.lstrip("@").lower()

    if not target_id:
        try:
            return await client.get_entity(parsed)
        except Exception:
            pass

    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        dialog_id = str(dialog.id)
        username = str(getattr(entity, "username", "") or "").lower()
        if target_id and dialog_id == target_id:
            return entity
        if target_username and username == target_username:
            return entity

    return await client.get_entity(parsed)


async def refresh_destination(
    sender: TelegramClient,
    store: DedupeStore,
    state: RuntimeState,
) -> None:
    destination_ref = store.get_config("destination_chat")
    state.destination_ref = destination_ref
    state.destination = None
    state.ignored_chat_ids = set()

    if not destination_ref:
        return

    destination = await sender.get_entity(parse_chat_ref(destination_ref))
    state.destination = destination
    state.ignored_chat_ids = {str(utils.get_peer_id(destination))}


async def chat_title(message: Message) -> str:
    chat = await message.get_chat()
    for attr in ("title", "first_name", "username"):
        value = getattr(chat, attr, None)
        if value:
            return str(value)
    return str(message.chat_id or "Telegram")


async def source_permalink(message: Message) -> str | None:
    chat = await message.get_chat()
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{message.id}"

    chat_id = str(message.chat_id or "")
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message.id}"
    return None


def matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        try:
            if re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
                return True
        except re.error as exc:
            LOGGER.warning("Ignoring invalid regex %r: %s", pattern, exc)
    return False


async def send_with_retry(
    sender: TelegramClient,
    destination: object,
    message: Message,
    text: str,
    copy_media: bool,
    limiter: RateLimiter,
) -> None:
    await limiter.wait()
    try:
        await send_once(sender, destination, message, text, copy_media)
    except FloodWaitError as exc:
        LOGGER.warning("Telegram flood wait: sleeping %s seconds", exc.seconds)
        await asyncio.sleep(exc.seconds + 1)
        await limiter.wait()
        await send_once(sender, destination, message, text, copy_media)


async def send_once(
    sender: TelegramClient,
    destination: object,
    message: Message,
    text: str,
    copy_media: bool,
) -> None:
    if copy_media and message.media:
        with tempfile.TemporaryDirectory(prefix="shopperbot-") as temp_dir:
            downloaded = await message.download_media(file=temp_dir)
            if downloaded:
                caption = trim_text(text, CAPTION_LIMIT)
                await sender.send_file(
                    destination,
                    file=Path(downloaded),
                    caption=caption,
                    parse_mode=None,
                )
                if len(text) > CAPTION_LIMIT:
                    await sender.send_message(destination, text, parse_mode=None)
                return

    await sender.send_message(
        destination,
        text,
        link_preview=True,
        parse_mode=None,
    )


async def handle_message(
    *,
    message: Message,
    settings: Settings,
    store: DedupeStore,
    sender: TelegramClient,
    destination: object | None,
    limiter: RateLimiter,
    ignored_chat_ids: set[str],
) -> None:
    source_id = str(message.chat_id or utils.get_peer_id(message.peer_id))
    message_id = int(message.id)
    if source_id in ignored_chat_ids:
        return
    if getattr(message, "out", False):
        store.mark_message(source_id, message_id)
        return
    if destination is None:
        LOGGER.info("Skipping %s/%s because no destination is configured", source_id, message_id)
        return
    if store.has_message(source_id, message_id):
        return

    raw_text = message.raw_text or ""
    if not raw_text and not message.media:
        store.mark_message(source_id, message_id)
        return

    searchable_text = raw_text or ""
    if settings.allow_patterns and not matches_any(searchable_text, settings.allow_patterns):
        store.mark_message(source_id, message_id)
        return
    if settings.skip_patterns and matches_any(searchable_text, settings.skip_patterns):
        store.mark_message(source_id, message_id)
        return

    fingerprint = build_fingerprint(raw_text, fallback=f"{source_id}:{message_id}")
    ttl_seconds = settings.dedupe_ttl_days * 24 * 60 * 60
    if not store.claim_fingerprint(fingerprint, source_id, message_id, ttl_seconds):
        LOGGER.info("Duplicate skipped from %s/%s", source_id, message_id)
        store.mark_message(source_id, message_id)
        return

    link = await source_permalink(message) if settings.include_source_link else None
    outbound = build_outbound_text(
        source_title=await chat_title(message),
        body=raw_text,
        source_link=link,
        max_chars=settings.max_text_chars,
    )

    try:
        if settings.dry_run:
            LOGGER.info("DRY_RUN message from %s/%s:\n%s", source_id, message_id, outbound)
        else:
            await send_with_retry(
                sender=sender,
                destination=destination,
                message=message,
                text=outbound,
                copy_media=settings.copy_media,
                limiter=limiter,
            )
        store.mark_message(source_id, message_id)
        LOGGER.info("Delivered message from %s/%s", source_id, message_id)
    except Exception:
        store.release_fingerprint(fingerprint)
        raise


async def run_backfill(
    *,
    client: TelegramClient,
    sources: list[object],
    settings: Settings,
    store: DedupeStore,
    sender: TelegramClient,
    destination: object | None,
    limiter: RateLimiter,
    ignored_chat_ids: set[str],
) -> None:
    if settings.startup_backfill_limit <= 0 or not sources:
        return

    LOGGER.info("Backfilling last %s messages per source", settings.startup_backfill_limit)
    for source in sources:
        async for message in client.iter_messages(
            source,
            limit=settings.startup_backfill_limit,
            reverse=True,
        ):
            await handle_message(
                message=message,
                settings=settings,
                store=store,
                sender=sender,
                destination=destination,
                limiter=limiter,
                ignored_chat_ids=ignored_chat_ids,
            )


def command_arg(raw_text: str) -> str:
    parts = raw_text.split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def control_help(user_id: int | None) -> str:
    lines = [
        "Shopper Easly Bot",
        "",
        f"Il tuo user id: {user_id or 'sconosciuto'}",
        "",
        "Comandi:",
        "/claim - diventa admin se il bot non e' ancora configurato",
        "/folder <link> - importa una cartella Telegram t.me/addlist/...",
        "/add <@username o id> - aggiunge una sorgente singola",
        "/find <testo> - cerca canali, gruppi e bot visibili dal tuo account",
        "/destination <@canale o id> - imposta dove pubblicare le offerte",
        "/destination_here - pubblica nella chat corrente",
        "/sources - mostra le sorgenti attive",
        "/clear_sources - svuota le sorgenti",
        "/status - stato del servizio",
        "/whoami - mostra user id e chat id",
    ]
    return "\n".join(lines)


async def is_control_admin(
    event: events.NewMessage.Event,
    settings: Settings,
    store: DedupeStore,
) -> bool:
    sender_id = int(event.sender_id or 0)
    if store.is_admin(sender_id, settings.admin_user_ids):
        return True

    await event.respond(
        "Non sei admin di questo bot. Usa /whoami e metti il tuo user id in "
        "ADMIN_USER_IDS, oppure usa /claim se il bot non e' ancora stato reclamato.",
        parse_mode=None,
    )
    return False


async def register_control_bot(
    *,
    bot: TelegramClient,
    source_client: TelegramClient,
    settings: Settings,
    store: DedupeStore,
    state: RuntimeState,
) -> None:
    @bot.on(events.NewMessage(pattern=r"^/(start|help)(?:@\w+)?(?:\s|$)"))
    async def on_help(event: events.NewMessage.Event) -> None:
        await event.respond(control_help(event.sender_id), parse_mode=None)

    @bot.on(events.NewMessage(pattern=r"^/whoami(?:@\w+)?(?:\s|$)"))
    async def on_whoami(event: events.NewMessage.Event) -> None:
        await event.respond(
            f"user_id={event.sender_id}\nchat_id={event.chat_id}",
            parse_mode=None,
        )

    @bot.on(events.NewMessage(pattern=r"^/claim(?:@\w+)?(?:\s|$)"))
    async def on_claim(event: events.NewMessage.Event) -> None:
        sender_id = int(event.sender_id or 0)
        if not sender_id:
            await event.respond("Non riesco a leggere il tuo user id.", parse_mode=None)
            return

        if settings.admin_user_ids and sender_id not in settings.admin_user_ids:
            await event.respond(
                "ADMIN_USER_IDS e' gia' configurato e il tuo user id non e' incluso.",
                parse_mode=None,
            )
            return

        if store.has_admins() and not store.is_admin(sender_id, settings.admin_user_ids):
            await event.respond(
                "Questo bot e' gia' stato reclamato da un admin.",
                parse_mode=None,
            )
            return

        store.add_admin(sender_id)
        await event.respond("Ok, sei admin del bot.", parse_mode=None)

    @bot.on(events.NewMessage(pattern=r"^/destination_here(?:@\w+)?(?:\s|$)"))
    async def on_destination_here(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return

        store.set_config("destination_chat", str(event.chat_id))
        await refresh_destination(bot, store, state)
        await event.respond("Destinazione impostata su questa chat.", parse_mode=None)

    @bot.on(events.NewMessage(pattern=r"^/destination(?:@\w+)?(?:\s|$)"))
    async def on_destination(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return

        destination_ref = command_arg(event.raw_text or "")
        if not destination_ref:
            current = state.destination_ref or "non impostata"
            await event.respond(f"Destinazione attuale: {current}", parse_mode=None)
            return

        store.set_config("destination_chat", destination_ref)
        try:
            await refresh_destination(bot, store, state)
        except Exception as exc:
            store.set_config("destination_chat", "")
            state.destination_ref = None
            state.destination = None
            await event.respond(
                "Non riesco ad accedere a quella destinazione. Aggiungi il bot al "
                f"canale/gruppo e riprova.\nErrore: {exc}",
                parse_mode=None,
            )
            return

        await event.respond(f"Destinazione impostata: {destination_ref}", parse_mode=None)

    @bot.on(events.NewMessage(pattern=r"^/folder(?:@\w+)?(?:\s|$)"))
    async def on_folder(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return

        link = command_arg(event.raw_text or "")
        if not link:
            await event.respond(
                "Mandami il link della cartella, per esempio:\n"
                "/folder https://t.me/addlist/...",
                parse_mode=None,
            )
            return

        await event.respond("Importo la cartella e aggiorno le sorgenti...", parse_mode=None)
        try:
            result = await import_chat_folder(source_client, link)
        except Exception as exc:
            await event.respond(f"Non sono riuscito a importare la cartella: {exc}", parse_mode=None)
            return

        for source in result.sources:
            store.add_source(source.peer_id, source.title, source.username)

        state.source_ids = store.source_ids()
        await event.respond(
            "Cartella importata.\n"
            f"Titolo: {result.title}\n"
            f"Sorgenti attive: {len(state.source_ids)}\n"
            f"Chat aggiunte/aggiornate da Telegram: {result.joined_count}",
            parse_mode=None,
        )

    @bot.on(events.NewMessage(pattern=r"^/find(?:@\w+)?(?:\s|$)"))
    async def on_find(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return

        query = command_arg(event.raw_text or "").lower()
        if not query:
            await event.respond(
                "Scrivi cosa cercare, per esempio:\n/find junction",
                parse_mode=None,
            )
            return

        matches = []
        async for dialog in source_client.iter_dialogs():
            entity = dialog.entity
            title = str(dialog.name or entity_title(entity))
            username = str(getattr(entity, "username", "") or "")
            searchable = f"{title} {username} {dialog.id}".lower()
            if query not in searchable:
                continue
            kind = "bot" if getattr(entity, "bot", False) else "chat"
            matches.append((title, username, dialog.id, kind))
            if len(matches) >= 30:
                break

        if not matches:
            await event.respond("Nessun risultato. Avvia prima quel bot/chat dal tuo account Telegram.", parse_mode=None)
            return

        lines = ["Risultati:", ""]
        for title, username, dialog_id, kind in matches:
            handle = f" @{username}" if username else ""
            lines.append(f"- [{kind}] {title}{handle}\n  /add {dialog_id}")
        await event.respond("\n".join(lines), parse_mode=None)

    @bot.on(events.NewMessage(pattern=r"^/add(?:@\w+)?(?:\s|$)"))
    async def on_add(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return

        source_ref = command_arg(event.raw_text or "")
        if not source_ref:
            await event.respond(
                "Mandami una sorgente, per esempio:\n/add @nomecanale",
                parse_mode=None,
            )
            return

        try:
            await maybe_join_source(source_client, parse_chat_ref(source_ref))
            entity = await resolve_dialog_ref(source_client, source_ref)
        except Exception as exc:
            await event.respond(f"Non riesco ad aggiungere la sorgente: {exc}", parse_mode=None)
            return

        save_source_entity(store, entity)
        state.source_ids = store.source_ids()
        await event.respond(
            f"Sorgente aggiunta: {entity_title(entity)} ({utils.get_peer_id(entity)})",
            parse_mode=None,
        )

    @bot.on(events.NewMessage(pattern=r"^/sources(?:@\w+)?(?:\s|$)"))
    async def on_sources(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return

        sources = store.list_sources()
        if not sources:
            await event.respond("Nessuna sorgente configurata. Usa /folder <link>.", parse_mode=None)
            return

        lines = [f"Sorgenti attive: {len(sources)}", ""]
        for source in sources[:40]:
            username = f" @{source.username}" if source.username else ""
            lines.append(f"- {source.title}{username} ({source.peer_id})")
        if len(sources) > 40:
            lines.append(f"... e altre {len(sources) - 40}")
        await event.respond("\n".join(lines), parse_mode=None)

    @bot.on(events.NewMessage(pattern=r"^/clear_sources(?:@\w+)?(?:\s|$)"))
    async def on_clear_sources(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return

        store.clear_sources()
        state.source_ids = set()
        await event.respond("Sorgenti svuotate. Usa /folder <link> per ricaricarle.", parse_mode=None)

    @bot.on(events.NewMessage(pattern=r"^/status(?:@\w+)?(?:\s|$)"))
    async def on_status(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return

        await event.respond(
            "\n".join(
                [
                    "Stato Shopper Easly",
                    f"Sorgenti: {len(state.source_ids)}",
                    f"Destinazione: {state.destination_ref or 'non impostata'}",
                    f"Monitor all chats: {settings.monitor_all_chats}",
                    f"Dry run: {settings.dry_run}",
                ]
            ),
            parse_mode=None,
        )


async def run(settings: Settings | None = None) -> None:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    LOGGER.info("Starting with config: %s", settings.as_log_safe_dict())

    store = DedupeStore(settings.database_path)
    source_client = TelegramClient(
        StringSession(settings.telegram_session),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    sender_client: TelegramClient | None = None

    try:
        await source_client.start()
        sender = source_client
        state = RuntimeState()

        if settings.telegram_bot_token:
            sender_client = TelegramClient(
                StringSession(),
                settings.telegram_api_id,
                settings.telegram_api_hash,
            )
            await sender_client.start(bot_token=settings.telegram_bot_token)
            sender = sender_client

        if settings.destination_chat is not None:
            store.set_config("destination_chat", str(settings.destination_chat))

        if settings.source_chats:
            env_sources = await resolve_sources(
                source_client,
                settings.source_chats,
                settings.join_sources,
            )
            for source in env_sources:
                save_source_entity(store, source)

        state.source_ids = store.source_ids()
        await refresh_destination(sender, store, state)

        if sender_client is not None:
            await register_control_bot(
                bot=sender_client,
                source_client=source_client,
                settings=settings,
                store=store,
                state=state,
            )

        limiter = RateLimiter(settings.min_post_interval_seconds)
        sources = await resolve_saved_sources(source_client, store)
        await run_backfill(
            client=source_client,
            sources=sources,
            settings=settings,
            store=store,
            sender=sender,
            destination=state.destination,
            limiter=limiter,
            ignored_chat_ids=state.ignored_chat_ids,
        )

        event = events.NewMessage(incoming=True)

        @source_client.on(event)
        async def on_new_message(new_message_event: events.NewMessage.Event) -> None:
            try:
                source_id = str(
                    new_message_event.message.chat_id
                    or utils.get_peer_id(new_message_event.message.peer_id)
                )
                if not settings.monitor_all_chats and source_id not in state.source_ids:
                    return

                await handle_message(
                    message=new_message_event.message,
                    settings=settings,
                    store=store,
                    sender=sender,
                    destination=state.destination,
                    limiter=limiter,
                    ignored_chat_ids=state.ignored_chat_ids,
                )
            except Exception:
                LOGGER.exception("Could not process incoming message")

        LOGGER.info("Shopper Easly bot is online")
        await source_client.run_until_disconnected()
    finally:
        store.close()
        await source_client.disconnect()
        if sender_client is not None:
            await sender_client.disconnect()


def main() -> None:
    try:
        asyncio.run(run())
    except ConfigError as exc:
        configure_logging("ERROR")
        LOGGER.error("%s", exc)
        raise SystemExit(2) from exc
    except AccessTokenInvalidError as exc:
        configure_logging("ERROR")
        LOGGER.error(
            "TELEGRAM_BOT_TOKEN non valido. Rigeneralo con BotFather e aggiornalo nel file .env."
        )
        raise SystemExit(2) from exc
    except ApiIdInvalidError as exc:
        configure_logging("ERROR")
        LOGGER.error(
            "TELEGRAM_API_ID/TELEGRAM_API_HASH non validi. Ricopiali da my.telegram.org/apps."
        )
        raise SystemExit(2) from exc
