from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Iterable

from telethon import TelegramClient

from .constants import PRIVATE_DELETE_CACHE_SECONDS, PRIVATE_DELETE_SCAN_LIMIT
from .dedupe import DedupeStore, OfferRecord
from .filters import passes_filters
from .runtime import (
    PublishedOfferMessage,
    bot_api_delete_messages,
    bot_api_request,
    bot_api_token,
    destination_peer_id,
    first_user_client,
    offer_activity_for_offer_urls,
    offer_record_as_published,
    parse_published_offer_message_fast,
    published_offers_match,
)


LOGGER = logging.getLogger("shopper_merge_bot")


@dataclass
class PrivateDialogCache:
    loaded_at: float
    user_client: TelegramClient
    bot_entity: object
    message_id_offset: int
    messages: list[PublishedOfferMessage]


PRIVATE_DIALOG_CACHES: dict[tuple[int, str], PrivateDialogCache] = {}


async def bot_api_get_me(token: str) -> dict[str, object] | None:
    result = await bot_api_request(token, "getMe", {})
    if not result.get("ok"):
        LOGGER.warning("Bot API getMe failed during private cleanup: %s", result.get("description", result))
        return None
    bot_user = result.get("result")
    return bot_user if isinstance(bot_user, dict) else None

async def detect_private_message_id_offset(
    *,
    user_client: TelegramClient,
    bot_entity: object,
    bot_token: str,
    destination: object,
) -> int | None:
    marker = f"Diagnosi Shopper Easly cleanup {int(time.time() * 1000)}"
    chat_id = destination_peer_id(destination)
    sent = await bot_api_request(
        bot_token,
        "sendMessage",
        {"chat_id": chat_id, "text": marker, "disable_notification": "true"},
    )
    if not sent.get("ok") or not isinstance(sent.get("result"), dict):
        LOGGER.warning("Could not send private cleanup marker: %s", sent.get("description", sent))
        return None

    bot_message_id = int(sent["result"]["message_id"])  # type: ignore[index]
    user_message_id: int | None = None
    for _ in range(10):
        async for message in user_client.iter_messages(bot_entity, limit=20):
            if (message.raw_text or "") == marker:
                user_message_id = int(message.id)
                break
        if user_message_id is not None:
            break
        await asyncio.sleep(0.5)

    await bot_api_delete_messages(bot_token, destination, [bot_message_id])
    if user_message_id is not None:
        try:
            await user_client.delete_messages(bot_entity, [user_message_id], revoke=False)
        except Exception as exc:
            LOGGER.debug("Could not remove user-side private cleanup marker: %s", exc)

    if user_message_id is None:
        LOGGER.warning("Could not find private cleanup marker in the user-side bot dialog")
        return None
    return user_message_id - bot_message_id

async def load_private_dialog_cache(
    *,
    senders: Iterable[TelegramClient],
    bot_token: str,
    destination: object,
) -> PrivateDialogCache | None:
    user_client = await first_user_client(tuple(senders))
    if user_client is None:
        LOGGER.warning("Could not clean private bot dialog: no user Telegram client is available")
        return None

    bot_user = await bot_api_get_me(bot_token)
    if bot_user is None:
        return None
    bot_ref = str(bot_user.get("username") or bot_user.get("id") or "")
    if not bot_ref:
        LOGGER.warning("Could not clean private bot dialog: bot username/id is unavailable")
        return None

    cache_key = (id(user_client), bot_ref)
    cached = PRIVATE_DIALOG_CACHES.get(cache_key)
    if cached is not None and time.monotonic() - cached.loaded_at < PRIVATE_DELETE_CACHE_SECONDS:
        return cached

    try:
        bot_entity = await user_client.get_entity(bot_ref)
    except Exception as exc:
        LOGGER.warning("Could not resolve user-side bot dialog %s: %s", bot_ref, exc)
        return None

    offset = await detect_private_message_id_offset(
        user_client=user_client,
        bot_entity=bot_entity,
        bot_token=bot_token,
        destination=destination,
    )
    if offset is None:
        return None

    messages: list[PublishedOfferMessage] = []
    try:
        async for message in user_client.iter_messages(bot_entity, limit=PRIVATE_DELETE_SCAN_LIMIT):
            published = parse_published_offer_message_fast(int(message.id), message.raw_text or "")
            if published is not None:
                messages.append(published)
    except Exception as exc:
        LOGGER.warning("Could not scan user-side private bot dialog for cleanup: %s", exc)

    cache = PrivateDialogCache(
        loaded_at=time.monotonic(),
        user_client=user_client,
        bot_entity=bot_entity,
        message_id_offset=offset,
        messages=messages,
    )
    PRIVATE_DIALOG_CACHES[cache_key] = cache
    LOGGER.info(
        "Loaded private bot dialog cleanup cache: offset=%s structured_messages=%s",
        offset,
        len(messages),
    )
    return cache

async def delete_private_bot_dialog_messages(
    *,
    senders: Iterable[TelegramClient],
    destination: object,
    bot_token: str,
    bot_message_ids: list[int],
    offer: OfferRecord | None,
) -> bool:
    cache = await load_private_dialog_cache(
        senders=senders,
        bot_token=bot_token,
        destination=destination,
    )
    if cache is None:
        return False

    user_message_ids = [
        int(message_id) + cache.message_id_offset
        for message_id in bot_message_ids
        if int(message_id) + cache.message_id_offset > 0
    ]
    deleted_any = False
    deleted_user_message_ids: set[int] = set()
    if user_message_ids:
        try:
            await cache.user_client.delete_messages(cache.bot_entity, user_message_ids, revoke=False)
            deleted_any = True
            deleted_user_message_ids.update(user_message_ids)
        except Exception as exc:
            LOGGER.warning("Could not delete user-side private bot messages %s: %s", user_message_ids, exc)

    if offer is not None:
        target = offer_record_as_published(offer)
        matching_ids = [
            message.message_id
            for message in cache.messages
            if published_offers_match(target, message)
            and message.message_id not in deleted_user_message_ids
        ]
        if matching_ids:
            try:
                await cache.user_client.delete_messages(cache.bot_entity, matching_ids, revoke=False)
                deleted_any = True
                deleted_user_message_ids.update(matching_ids)
            except Exception as exc:
                LOGGER.warning("Could not delete matching private bot messages %s: %s", matching_ids, exc)

    if deleted_any:
        cache.messages = [
            message for message in cache.messages if message.message_id not in deleted_user_message_ids
        ]
        LOGGER.info(
            "Deleted private bot dialog messages locally: bot_ids=%s user_ids=%s",
            bot_message_ids,
            sorted(deleted_user_message_ids),
        )
    return deleted_any

async def purge_inactive_private_published_messages(
    *,
    senders: Iterable[TelegramClient],
    destination: object,
    store: DedupeStore,
    limit: int,
) -> tuple[int, int, int, int, int]:
    token = bot_api_token()
    if token is None:
        return 0, 0, 0, 0, 1

    cache = await load_private_dialog_cache(
        senders=senders,
        bot_token=token,
        destination=destination,
    )
    if cache is None:
        return 0, 0, 0, 0, 1

    scanned = 0
    deleted = 0
    active = 0
    unknown = 0
    failed = 0
    scanned_messages = cache.messages if limit <= 0 else cache.messages[:limit]
    delete_reasons: dict[int, str] = {}
    for published in scanned_messages:
        scanned += 1
        try:
            activity = await offer_activity_for_offer_urls(
                published.urls,
                published.current_price,
            )
        except Exception as exc:
            LOGGER.warning("Could not check private published offer %s: %s", published.message_id, exc)
            failed += 1
            continue

        if activity.status == "inactive":
            delete_reasons[published.message_id] = activity.reason
        elif activity.status == "active":
            active += 1
        else:
            unknown += 1

    deleted_ids: set[int] = set()
    sorted_ids = sorted(delete_reasons)
    for start in range(0, len(sorted_ids), 100):
        chunk = sorted_ids[start : start + 100]
        try:
            await cache.user_client.delete_messages(cache.bot_entity, chunk, revoke=False)
            deleted += len(chunk)
            deleted_ids.update(chunk)
        except Exception as exc:
            LOGGER.warning("Could not purge private expired offers chunk %s: %s", chunk[:5], exc)
            failed += len(chunk)

    if deleted_ids:
        active_by_fingerprint = {
            offer.fingerprint: offer
            for offer in store.list_active_offers()
        }
        for message in cache.messages:
            if message.message_id not in deleted_ids:
                continue
            tracked = active_by_fingerprint.get(message.fingerprint)
            if tracked is not None:
                store.mark_offer_status(
                    tracked.fingerprint,
                    f"deleted:expired-private:{delete_reasons[message.message_id]}",
                )
        cache.messages = [
            message for message in cache.messages if message.message_id not in deleted_ids
        ]

    return scanned, deleted, active, unknown, failed

async def purge_unmerged_private_bot_dialog_messages(
    *,
    senders: Iterable[TelegramClient],
    destination: object,
    store: DedupeStore,
    limit: int,
) -> tuple[int, int, int]:
    token = bot_api_token()
    if token is None:
        return 0, 0, 1

    cache = await load_private_dialog_cache(
        senders=senders,
        bot_token=token,
        destination=destination,
    )
    if cache is None:
        return 0, 0, 1

    groups: list[list[PublishedOfferMessage]] = []
    for published in cache.messages[:limit]:
        for group in groups:
            if any(published_offers_match(published, existing) for existing in group):
                group.append(published)
                break
        else:
            groups.append([published])

    active_by_user_message_id = {
        offer.primary_message_id + cache.message_id_offset: offer
        for offer in store.list_active_offers()
    }

    deleted = 0
    groups_with_duplicates = 0
    failed = 0
    removed_ids: set[int] = set()
    for group in groups:
        if len(group) < 2:
            continue
        groups_with_duplicates += 1
        group_ids = {item.message_id for item in group}
        tracked_ids = [
            item.message_id
            for item in group
            if item.message_id in active_by_user_message_id
        ]
        keep_id = tracked_ids[0] if tracked_ids else max(group_ids)
        delete_ids = sorted(group_ids - {keep_id})
        if not delete_ids:
            continue
        try:
            await cache.user_client.delete_messages(cache.bot_entity, delete_ids, revoke=False)
            deleted += len(delete_ids)
            removed_ids.update(delete_ids)
            for message_id in delete_ids:
                tracked = active_by_user_message_id.get(message_id)
                if tracked is not None:
                    store.mark_offer_status(tracked.fingerprint, "deleted:unmerged-destination")
        except Exception as exc:
            LOGGER.warning("Could not purge private unmerged messages %s: %s", delete_ids, exc)
            failed += len(delete_ids)

    if removed_ids:
        cache.messages = [
            message for message in cache.messages if message.message_id not in removed_ids
        ]
    return deleted, groups_with_duplicates, failed

def message_passes_history_filters(store: DedupeStore, message: PublishedOfferMessage) -> bool:
    return passes_filters(store, message.category or "altro", message.current_price)

async def purge_private_structured_offer_messages(
    *,
    senders: Iterable[TelegramClient],
    destination: object,
    limit: int,
) -> tuple[int, int, int]:
    token = bot_api_token()
    if token is None:
        return 0, 0, 1

    cache = await load_private_dialog_cache(
        senders=senders,
        bot_token=token,
        destination=destination,
    )
    if cache is None:
        return 0, 0, 1

    scanned_messages = cache.messages if limit <= 0 else cache.messages[:limit]
    delete_ids = sorted({message.message_id for message in scanned_messages})
    if not delete_ids:
        return len(scanned_messages), 0, 0

    deleted = 0
    failed = 0
    deleted_ids: set[int] = set()
    for start in range(0, len(delete_ids), 100):
        chunk = delete_ids[start : start + 100]
        try:
            await cache.user_client.delete_messages(cache.bot_entity, chunk, revoke=False)
            deleted += len(chunk)
            deleted_ids.update(chunk)
        except Exception as exc:
            LOGGER.warning("Could not purge private structured offer messages %s: %s", chunk[:5], exc)
            failed += len(chunk)

    if deleted_ids:
        cache.messages = [
            message for message in cache.messages if message.message_id not in deleted_ids
        ]
    return len(scanned_messages), deleted, failed

async def purge_private_history_messages(
    *,
    senders: Iterable[TelegramClient],
    destination: object,
    store: DedupeStore,
    limit: int,
    keep_latest: int | None,
) -> tuple[int, int, int, int, int, int]:
    token = bot_api_token()
    if token is None:
        return 0, 0, 0, 0, 0, 1

    cache = await load_private_dialog_cache(
        senders=senders,
        bot_token=token,
        destination=destination,
    )
    if cache is None:
        return 0, 0, 0, 0, 0, 1

    active_by_user_message_id = {
        offer.primary_message_id + cache.message_id_offset: offer
        for offer in store.list_active_offers()
    }
    scanned_messages = cache.messages[:limit]
    seen_fingerprints: set[str] = set()
    kept = 0
    delete_reasons: dict[int, str] = {}
    deleted_filtered = 0
    deleted_duplicates = 0
    deleted_trimmed = 0

    for message in scanned_messages:
        reason = ""
        if not message_passes_history_filters(store, message):
            reason = "history-filtered"
            deleted_filtered += 1
        elif message.fingerprint in seen_fingerprints:
            reason = "history-duplicate"
            deleted_duplicates += 1
        elif keep_latest is not None and kept >= keep_latest:
            reason = "history-trimmed"
            deleted_trimmed += 1
        else:
            kept += 1
            seen_fingerprints.add(message.fingerprint)

        if reason:
            delete_reasons[message.message_id] = reason

    if not delete_reasons:
        return len(scanned_messages), 0, 0, 0, kept, 0

    failed = 0
    deleted_ids: set[int] = set()
    sorted_ids = sorted(delete_reasons)
    for start in range(0, len(sorted_ids), 100):
        chunk = sorted_ids[start : start + 100]
        try:
            await cache.user_client.delete_messages(cache.bot_entity, chunk, revoke=False)
            deleted_ids.update(chunk)
        except Exception as exc:
            LOGGER.warning("Could not purge private history chunk %s: %s", chunk[:5], exc)
            failed += len(chunk)

    for message_id in deleted_ids:
        tracked = active_by_user_message_id.get(message_id)
        if tracked is not None:
            store.mark_offer_status(tracked.fingerprint, f"deleted:{delete_reasons[message_id]}")

    if deleted_ids:
        cache.messages = [
            message for message in cache.messages if message.message_id not in deleted_ids
        ]
    return (
        len(scanned_messages),
        deleted_filtered,
        deleted_duplicates,
        deleted_trimmed,
        kept,
        failed,
    )
