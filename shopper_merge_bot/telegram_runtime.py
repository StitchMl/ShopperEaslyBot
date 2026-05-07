from __future__ import annotations

import logging
import re
from typing import Iterable

from telethon import TelegramClient, utils
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import Channel, Chat, Message, User

from .config import parse_chat_ref
from .dedupe import DedupeStore
from .runtime import RuntimeState


LOGGER = logging.getLogger("shopper_merge_bot")


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

def entity_kind(entity: object) -> str:
    if isinstance(entity, User) and getattr(entity, "bot", False):
        return "bot"
    if isinstance(entity, Channel) and getattr(entity, "broadcast", False):
        return "channel"
    if isinstance(entity, (Channel, Chat)):
        return "group"
    return "chat"

def entity_peer_id(entity: object) -> str:
    return str(utils.get_peer_id(entity))

def is_control_bot_entity(state: RuntimeState, entity: object) -> bool:
    return state.control_bot_peer_id is not None and entity_peer_id(entity) == state.control_bot_peer_id

def save_source_entity(store: DedupeStore, entity: object) -> None:
    store.add_source(
        peer_id=entity_peer_id(entity),
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
    if state.control_bot_peer_id is not None:
        state.ignored_chat_ids.add(state.control_bot_peer_id)

    if not destination_ref:
        return

    destination = await sender.get_entity(parse_chat_ref(destination_ref))
    state.destination = destination
    state.ignored_chat_ids.add(entity_peer_id(destination))

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

def destination_peer_id(destination: object) -> str:
    return str(utils.get_peer_id(destination))

def deleted_event_source_ids(chat_id: object | None) -> tuple[str, ...]:
    if chat_id is None:
        return ()
    candidates: list[str] = []

    def add(value: object) -> None:
        text = str(value)
        if text and text not in candidates:
            candidates.append(text)

    add(chat_id)
    try:
        numeric = int(chat_id)
    except (TypeError, ValueError):
        return tuple(candidates)

    if numeric > 0:
        add(f"-100{numeric}")
    elif numeric < 0 and not str(numeric).startswith("-100"):
        add(f"-100{abs(numeric)}")
    return tuple(candidates)

def message_source_ids(message: Message) -> tuple[str, ...]:
    return deleted_event_source_ids(message.chat_id or utils.get_peer_id(message.peer_id))

def preferred_source_id(source_ids: tuple[str, ...], known_source_ids: set[str]) -> str:
    for source_id in source_ids:
        if source_id in known_source_ids:
            return source_id
    return source_ids[0] if source_ids else ""

def mark_seen_message(store: DedupeStore, source_ids: tuple[str, ...], message_id: int) -> None:
    for source_id in source_ids:
        store.mark_message(source_id, message_id)
