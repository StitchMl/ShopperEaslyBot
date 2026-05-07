from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable

from telethon import TelegramClient
from telethon.tl.types import User

from . import runtime as runtime_api
from .constants import BOT_API_BASE, CAPTION_LIMIT
from .dedupe import DedupeStore, OfferRecord
from .formatter import trim_text
from .runtime import delete_private_bot_dialog_messages, destination_peer_id


LOGGER = logging.getLogger("shopper_merge_bot")


def bot_api_token(explicit_token: str | None = None) -> str | None:
    token = explicit_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
    token = token.strip()
    return token or None

def is_private_user_destination(destination: object) -> bool:
    return isinstance(destination, User)

def bot_api_request_sync(
    token: str,
    method: str,
    params: dict[str, object],
) -> dict[str, object]:
    data = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(f"{BOT_API_BASE}{token}/{method}", data=data)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"ok": False, "description": str(exc)}
        parsed.setdefault("ok", False)
        parsed.setdefault("error_code", exc.code)
        return parsed
    except Exception as exc:
        return {"ok": False, "description": str(exc)}

async def bot_api_request(
    token: str,
    method: str,
    params: dict[str, object],
) -> dict[str, object]:
    return await asyncio.to_thread(bot_api_request_sync, token, method, params)

def bot_api_not_modified(result: dict[str, object]) -> bool:
    description = str(result.get("description", "")).lower()
    return "message is not modified" in description

def bot_api_message_already_absent(result: dict[str, object]) -> bool:
    description = str(result.get("description", "")).lower()
    return "message to delete not found" in description

async def bot_api_edit_message(
    token: str,
    destination: object,
    message_id: int,
    text: str,
) -> bool:
    chat_id = destination_peer_id(destination)
    edited_text = trim_text(text, CAPTION_LIMIT)
    text_result = await runtime_api.bot_api_request(
        token,
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": edited_text,
            "disable_web_page_preview": "false",
        },
    )
    if text_result.get("ok") or bot_api_not_modified(text_result):
        return True

    caption_result = await runtime_api.bot_api_request(
        token,
        "editMessageCaption",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "caption": edited_text,
        },
    )
    if caption_result.get("ok") or bot_api_not_modified(caption_result):
        return True

    LOGGER.warning(
        "Bot API could not edit message %s: text_error=%s caption_error=%s",
        message_id,
        text_result.get("description", text_result),
        caption_result.get("description", caption_result),
    )
    return False

async def bot_api_delete_messages(
    token: str,
    destination: object,
    ids: list[int],
) -> bool:
    chat_id = destination_peer_id(destination)
    failed = False
    for message_id in ids:
        result = await runtime_api.bot_api_request(
            token,
            "deleteMessage",
            {"chat_id": chat_id, "message_id": int(message_id)},
        )
        if result.get("ok") or bot_api_message_already_absent(result):
            continue
        failed = True
        LOGGER.warning(
            "Bot API could not delete message %s: %s",
            message_id,
            result.get("description", result),
        )
    return not failed

async def edit_offer_message(
    senders: Iterable[TelegramClient],
    destination: object,
    offer: OfferRecord,
    text: str,
    bot_token: str | None = None,
) -> bool:
    token = bot_api_token(bot_token)
    if token is not None:
        if await bot_api_edit_message(token, destination, offer.primary_message_id, text):
            return True
        if is_private_user_destination(destination):
            return False

    for sender in senders:
        try:
            await sender.edit_message(
                destination,
                offer.primary_message_id,
                trim_text(text, CAPTION_LIMIT),
                parse_mode=None,
                link_preview=True,
            )
            return True
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return True
            LOGGER.warning(
                "Could not edit offer %s with %s: %s",
                offer.fingerprint,
                sender.session.__class__.__name__,
                exc,
            )
    return False

async def delete_messages_with_fallback(
    senders: Iterable[TelegramClient],
    destination: object,
    ids: list[int],
    bot_token: str | None = None,
    offer: OfferRecord | None = None,
) -> bool:
    ids = [int(item) for item in ids if item]
    if not ids:
        return True

    token = bot_api_token(bot_token)
    if token is not None:
        if await runtime_api.bot_api_delete_messages(token, destination, ids):
            return True
        if is_private_user_destination(destination):
            if await delete_private_bot_dialog_messages(
                senders=senders,
                destination=destination,
                bot_token=token,
                bot_message_ids=ids,
                offer=offer,
            ):
                return True
            return False

    for sender in senders:
        try:
            await sender.delete_messages(destination, ids, revoke=True)
            return True
        except Exception as exc:
            LOGGER.warning(
                "Could not delete messages %s with %s: %s",
                ids,
                sender.session.__class__.__name__,
                exc,
            )
    return False

async def delete_offer(
    senders: Iterable[TelegramClient],
    destination: object | None,
    store: DedupeStore,
    fingerprint: str,
    reason: str,
    *,
    include_inactive: bool = False,
) -> bool:
    offer = store.get_offer(fingerprint)
    if offer is None:
        return False
    if offer.status != "active" and not include_inactive:
        return False
    if destination is None:
        LOGGER.warning("Could not delete offer %s because destination is not configured", fingerprint)
        return False

    ids = [offer.primary_message_id, *offer.extra_message_ids]
    if not await runtime_api.delete_messages_with_fallback(senders, destination, ids, offer=offer):
        LOGGER.error("Could not delete offer %s after trying all clients", fingerprint)
        return False

    store.mark_offer_status(fingerprint, f"deleted:{reason}")
    return True
