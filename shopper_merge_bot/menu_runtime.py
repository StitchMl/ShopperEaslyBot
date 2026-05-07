from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from pathlib import Path
from typing import Iterable

from telethon import TelegramClient

from .constants import CAPTION_LIMIT, PRIVATE_DELETE_SCAN_LIMIT, PRODUCT_GIF_UPLOAD_TIMEOUT_SECONDS
from .dedupe import DedupeStore, MenuMessage, OfferRecord
from .formatter import trim_text
from .media import (
    create_product_gif,
    download_message_media,
    get_source_message,
    message_ids_from_result as _message_ids_from_result,
)
from .menu import (
    MENU_INDEX_KEY,
    MENU_OPEN_PREFIX,
    menu_expansion_close_buttons,
    menu_title_for_key,
    offer_menu_key,
    open_menu_storage_key,
    render_menu_index_text,
    render_offer_menu_detail_text,
    render_offer_menu_text,
    menu_index_buttons,
    mark_menu_group_seen,
)
from .offers import build_offer_publish_text_from_body
from .runtime import (
    delete_messages_with_fallback,
    is_private_user_destination,
    purge_private_structured_offer_messages,
)


LOGGER = logging.getLogger("shopper_merge_bot")


async def send_menu_message(
    sender: TelegramClient,
    destination: object,
    text: str,
    buttons: object | None = None,
) -> list[int]:
    result = await sender.send_message(
        destination,
        text,
        buttons=buttons,
        link_preview=False,
        parse_mode=None,
    )
    return _message_ids_from_result(result)

async def edit_menu_message(
    senders: Iterable[TelegramClient],
    destination: object,
    menu: MenuMessage,
    text: str,
    buttons: object | None = None,
) -> bool:
    for sender in senders:
        try:
            await sender.edit_message(
                destination,
                menu.message_id,
                text,
                buttons=buttons,
                parse_mode=None,
                link_preview=False,
            )
            return True
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return True
            LOGGER.warning(
                "Could not edit menu %s with %s: %s",
                menu.menu_key,
                sender.session.__class__.__name__,
                exc,
            )
    return False

async def upsert_offer_menu(
    *,
    senders: Iterable[TelegramClient],
    destination: object,
    store: DedupeStore,
    menu_key: str,
    offers: list[OfferRecord],
    max_chars: int,
) -> bool:
    if not offers:
        return await delete_offer_menu(
            senders=senders,
            destination=destination,
            store=store,
            menu_key=menu_key,
        )
    title = menu_title_for_key(menu_key, offers)
    text = render_offer_menu_text(store, menu_key, offers, max_chars)
    menu = store.get_menu_message(menu_key)
    senders_tuple = tuple(senders)
    if menu is not None and menu.message_id:
        if await edit_menu_message(senders_tuple, destination, menu, text):
            store.save_menu_message(
                menu_key=menu_key,
                message_id=menu.message_id,
                extra_message_ids=menu.extra_message_ids,
                title=title,
            )
            return True
        store.delete_menu_message(menu_key)

    sender = senders_tuple[0] if senders_tuple else None
    if sender is None:
        return False
    try:
        message_ids = await send_menu_message(sender, destination, text)
    except Exception as exc:
        LOGGER.warning("Could not send menu %s: %s", menu_key, exc)
        return False
    if not message_ids:
        return False
    store.save_menu_message(
        menu_key=menu_key,
        message_id=message_ids[0],
        extra_message_ids=tuple(message_ids[1:]),
        title=title,
    )
    return True

async def upsert_menu_index(
    *,
    senders: Iterable[TelegramClient],
    destination: object,
    store: DedupeStore,
) -> bool:
    text = render_menu_index_text(store)
    buttons = menu_index_buttons(store) or None
    title = "Menu offerte"
    menu = store.get_menu_message(MENU_INDEX_KEY)
    senders_tuple = tuple(senders)
    if menu is not None and menu.message_id:
        if await edit_menu_message(senders_tuple, destination, menu, text, buttons=buttons):
            store.save_menu_message(
                menu_key=MENU_INDEX_KEY,
                message_id=menu.message_id,
                extra_message_ids=menu.extra_message_ids,
                title=title,
            )
            return True
        store.delete_menu_message(MENU_INDEX_KEY)

    sender = senders_tuple[0] if senders_tuple else None
    if sender is None:
        return False
    try:
        message_ids = await send_menu_message(sender, destination, text, buttons=buttons)
    except Exception as exc:
        LOGGER.warning("Could not send menu index: %s", exc)
        return False
    if not message_ids:
        return False
    store.save_menu_message(
        menu_key=MENU_INDEX_KEY,
        message_id=message_ids[0],
        extra_message_ids=tuple(message_ids[1:]),
        title=title,
    )
    return True

async def delete_legacy_offer_menus(
    *,
    senders: Iterable[TelegramClient],
    destination: object,
    store: DedupeStore,
) -> tuple[int, int]:
    deleted = 0
    failed = 0
    for menu in store.list_menu_messages():
        if menu.menu_key == MENU_INDEX_KEY:
            continue
        if await delete_offer_menu(
            senders=senders,
            destination=destination,
            store=store,
            menu_key=menu.menu_key,
        ):
            deleted += 1
        else:
            failed += 1
    return deleted, failed

async def delete_offer_menu(
    *,
    senders: Iterable[TelegramClient],
    destination: object,
    store: DedupeStore,
    menu_key: str,
) -> bool:
    menu = store.get_menu_message(menu_key)
    if menu is None:
        return True
    ids = [menu.message_id, *menu.extra_message_ids]
    deleted = True
    for start in range(0, len(ids), 100):
        chunk = ids[start : start + 100]
        if not await delete_messages_with_fallback(senders, destination, chunk):
            deleted = False
    if deleted:
        store.delete_menu_message(menu_key)
    return deleted

async def delete_open_menu_expansions(
    *,
    senders: Iterable[TelegramClient],
    destination: object,
    store: DedupeStore,
    except_menu_key: str | None = None,
) -> tuple[int, int]:
    deleted = 0
    failed = 0
    except_storage_key = open_menu_storage_key(except_menu_key) if except_menu_key else ""
    for menu in store.list_menu_messages():
        if not menu.menu_key.startswith(MENU_OPEN_PREFIX):
            continue
        if except_storage_key and menu.menu_key == except_storage_key:
            continue
        if await delete_offer_menu(
            senders=senders,
            destination=destination,
            store=store,
            menu_key=menu.menu_key,
        ):
            deleted += 1
        else:
            failed += 1
    return deleted, failed

def build_offer_detail_text(store: DedupeStore, offer: OfferRecord, max_chars: int) -> str:
    return build_offer_publish_text_from_body(
        body=offer.text,
        category=offer.category,
        sources=store.offer_sources(offer.fingerprint),
        max_chars=max_chars,
    )

async def collect_offer_media_paths(
    *,
    source_reader: TelegramClient,
    store: DedupeStore,
    offer: OfferRecord,
    directory: Path,
) -> list[Path]:
    media_paths: list[Path] = []
    seen_source_messages: set[tuple[str, int]] = set()
    for source in store.offer_source_messages(offer.fingerprint):
        source_key = (source.source_chat_id, source.source_message_id)
        if source_key in seen_source_messages:
            continue
        seen_source_messages.add(source_key)
        source_message = await get_source_message(source_reader, source)
        if source_message is None or not getattr(source_message, "media", None):
            continue
        source_dir = directory / f"source-{len(media_paths)}"
        source_dir.mkdir()
        source_media = await download_message_media(source_message, source_dir)
        if source_media is not None:
            media_paths.append(source_media)
    return media_paths

async def send_offer_detail_message(
    *,
    sender: TelegramClient,
    source_reader: TelegramClient,
    destination: object,
    store: DedupeStore,
    offer: OfferRecord,
    max_chars: int,
) -> list[int]:
    text = build_offer_detail_text(store, offer, max_chars)
    with tempfile.TemporaryDirectory(prefix="shopperbot-menu-offer-") as temp_dir:
        temp_path = Path(temp_dir)
        media_paths = await collect_offer_media_paths(
            source_reader=source_reader,
            store=store,
            offer=offer,
            directory=temp_path,
        )
        media_path: Path | None = None
        gif_path = temp_path / "product-images.gif"
        if create_product_gif(media_paths, gif_path):
            media_path = gif_path
        elif media_paths:
            media_path = media_paths[0]

        if media_path is not None:
            try:
                result = await asyncio.wait_for(
                    sender.send_file(
                        destination,
                        file=media_path,
                        caption=trim_text(text, CAPTION_LIMIT),
                        parse_mode=None,
                    ),
                    timeout=PRODUCT_GIF_UPLOAD_TIMEOUT_SECONDS,
                )
                message_ids = _message_ids_from_result(result)
                if message_ids:
                    return message_ids
            except Exception as exc:
                LOGGER.warning("Could not send menu offer %s with media: %s", offer.fingerprint, exc)

    result = await sender.send_message(
        destination,
        text,
        link_preview=True,
        parse_mode=None,
    )
    return _message_ids_from_result(result)

async def expand_offer_menu(
    *,
    senders: Iterable[TelegramClient],
    source_reader: TelegramClient,
    destination: object,
    store: DedupeStore,
    menu_key: str,
    offers: list[OfferRecord],
    max_chars: int,
    pause_seconds: float,
) -> tuple[int, int]:
    await delete_open_menu_expansions(
        senders=senders,
        destination=destination,
        store=store,
    )

    senders_tuple = tuple(senders)
    sender = senders_tuple[0] if senders_tuple else None
    if sender is None:
        return 0, 1

    title = menu_title_for_key(menu_key, offers)
    sent_ids: list[int] = []
    sent_offers = 0
    failed = 0
    if offers:
        for offer in offers:
            try:
                message_ids = await send_offer_detail_message(
                    sender=sender,
                    source_reader=source_reader,
                    destination=destination,
                    store=store,
                    offer=offer,
                    max_chars=max_chars,
                )
                if message_ids:
                    sent_ids.extend(message_ids)
                    sent_offers += 1
                else:
                    failed += 1
            except Exception as exc:
                LOGGER.warning("Could not expand menu offer %s: %s", offer.fingerprint, exc)
                failed += 1
            if pause_seconds > 0:
                await asyncio.sleep(pause_seconds)

    if offers:
        control_text = "\n".join(
            [
                f"Shopper Easly - {title}",
                f"Prodotti mostrati: {sent_offers}",
            ]
        )
    else:
        control_text = render_offer_menu_detail_text(store, menu_key, offers, 0, max_chars)

    try:
        control_ids = await send_menu_message(
            sender,
            destination,
            control_text,
            buttons=menu_expansion_close_buttons(menu_key),
        )
    except Exception as exc:
        LOGGER.warning("Could not send menu expansion close control for %s: %s", menu_key, exc)
        return sent_offers, failed + 1

    if control_ids:
        store.save_menu_message(
            menu_key=open_menu_storage_key(menu_key),
            message_id=control_ids[0],
            extra_message_ids=tuple([*sent_ids, *control_ids[1:]]),
            title=title,
        )
    seen_at = max(
        (store.offer_latest_source_at(offer.fingerprint) for offer in offers),
        default=int(time.time()),
    )
    mark_menu_group_seen(store, menu_key, seen_at)
    return sent_offers, failed

async def sync_offer_menus(
    *,
    senders: Iterable[TelegramClient],
    destination: object | None,
    store: DedupeStore,
    max_chars: int,
    menu_keys: set[str] | None = None,
) -> tuple[int, int, int]:
    if destination is None:
        return 0, 0, 1
    updated = 0
    deleted, failed = await delete_legacy_offer_menus(
        senders=senders,
        destination=destination,
        store=store,
    )
    if await upsert_menu_index(
        senders=senders,
        destination=destination,
        store=store,
    ):
        updated += 1
    else:
        failed += 1
    return updated, deleted, failed

async def sync_offer_menu_for_offer(
    *,
    senders: Iterable[TelegramClient],
    destination: object | None,
    store: DedupeStore,
    offer: OfferRecord,
    max_chars: int,
    previous_menu_key: str | None = None,
) -> tuple[int, int, int]:
    menu_keys = {offer_menu_key(offer)}
    if previous_menu_key:
        menu_keys.add(previous_menu_key)
    return await sync_offer_menus(
        senders=senders,
        destination=destination,
        store=store,
        max_chars=max_chars,
        menu_keys=menu_keys,
    )

async def migrate_active_posts_to_menu_only(
    *,
    senders: Iterable[TelegramClient],
    destination: object | None,
    store: DedupeStore,
    max_chars: int,
) -> tuple[int, int, int, int]:
    if destination is None:
        return 0, 0, 0, 1
    menu_updated, _menu_deleted, menu_failed = await sync_offer_menus(
        senders=senders,
        destination=destination,
        store=store,
        max_chars=max_chars,
    )
    menu_ids = {
        menu.message_id
        for menu in store.list_menu_messages()
        if menu.message_id
    }
    offer_ids: dict[int, str] = {}
    for offer in store.list_active_offers():
        ids = [offer.primary_message_id, *offer.extra_message_ids]
        for message_id in ids:
            if message_id and message_id not in menu_ids:
                offer_ids[int(message_id)] = offer.fingerprint

    deleted_posts = 0
    failed = menu_failed
    ids = sorted(offer_ids)
    for start in range(0, len(ids), 100):
        chunk = ids[start : start + 100]
        if await delete_messages_with_fallback(senders, destination, chunk):
            deleted_posts += len(chunk)
            for fingerprint in {offer_ids[message_id] for message_id in chunk}:
                store.update_offer_delivery(fingerprint, 0, ())
        else:
            failed += len(chunk)
    legacy_scanned = 0
    if is_private_user_destination(destination):
        legacy_scanned, legacy_deleted, legacy_failed = await purge_private_structured_offer_messages(
            senders=senders,
            destination=destination,
            limit=PRIVATE_DELETE_SCAN_LIMIT,
        )
        deleted_posts += legacy_deleted
        failed += legacy_failed
    return menu_updated, len(ids) + legacy_scanned, deleted_posts, failed
