from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from telethon import TelegramClient, events
from telethon.errors import AccessTokenInvalidError, ApiIdInvalidError, FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import Message

from . import __version__
from .config import ConfigError, Settings
from .constants import (
    CAPTION_LIMIT,
    PRIVATE_DELETE_SCAN_LIMIT,
    PRODUCT_GIF_UPLOAD_TIMEOUT_SECONDS,
)
from .dedupe import DedupeStore, OfferRecord
from .filters import passes_filters
from .formatter import trim_text
from .media import (
    create_product_gif,
    download_message_media,
    edit_offer_media_as_gif,
    get_destination_message_with_media,
    get_source_message,
    message_ids_from_result as _message_ids_from_result,
    send_with_retry,
)
from .menu import (
    is_menu_only_enabled,
    mark_menu_group_seen,
    menu_group_summaries,
    offer_menu_key,
    offer_menu_type,
    parse_menu_callback_data,
    publish_mode,
    render_menu_index_text,
    render_offer_menu_detail_text,
    render_offer_menu_text,
    set_publish_mode,
)
from .normalization import resolve_redirect_url
from .offers import (
    build_offer_publish_text,
    build_offer_publish_text_from_body,
    extract_offer_label,
    fingerprint_for_offer_url,
    is_structured_offer_text,
    offer_record_original_price,
    offer_record_product,
    offer_record_urls,
    product_similarity,
    stable_offer_body,
)
from .offer_analysis import (
    known_filter_categories,
    parse_price_limit,
)
from .site_context import OfferActivity, combined_site_context, offer_activity_for_url


LOGGER = logging.getLogger("shopper_merge_bot")


@dataclass
class RuntimeState:
    source_ids: set[str] = field(default_factory=set)
    destination_ref: str | None = None
    destination: object | None = None
    ignored_chat_ids: set[str] = field(default_factory=set)
    control_bot_peer_id: str | None = None
    offer_processing_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True)
class PublishedOfferMessage:
    message_id: int
    fingerprint: str
    urls: tuple[str, ...]
    product: str | None
    original_price: Decimal | None
    current_price: Decimal | None
    category: str | None = None




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


def invite_hash(*args: object, **kwargs: object) -> str | None:
    from .telegram_runtime import invite_hash as impl

    return impl(*args, **kwargs)


async def maybe_join_source(*args: object, **kwargs: object) -> None:
    from .telegram_runtime import maybe_join_source as impl

    return await impl(*args, **kwargs)


async def resolve_sources(*args: object, **kwargs: object) -> list[object]:
    from .telegram_runtime import resolve_sources as impl

    return await impl(*args, **kwargs)


def entity_title(*args: object, **kwargs: object) -> str:
    from .telegram_runtime import entity_title as impl

    return impl(*args, **kwargs)


def entity_kind(*args: object, **kwargs: object) -> str:
    from .telegram_runtime import entity_kind as impl

    return impl(*args, **kwargs)


def entity_peer_id(*args: object, **kwargs: object) -> str:
    from .telegram_runtime import entity_peer_id as impl

    return impl(*args, **kwargs)


def is_control_bot_entity(*args: object, **kwargs: object) -> bool:
    from .telegram_runtime import is_control_bot_entity as impl

    return impl(*args, **kwargs)


def save_source_entity(*args: object, **kwargs: object) -> None:
    from .telegram_runtime import save_source_entity as impl

    return impl(*args, **kwargs)


async def resolve_saved_sources(*args: object, **kwargs: object) -> list[object]:
    from .telegram_runtime import resolve_saved_sources as impl

    return await impl(*args, **kwargs)


async def resolve_dialog_ref(*args: object, **kwargs: object) -> object:
    from .telegram_runtime import resolve_dialog_ref as impl

    return await impl(*args, **kwargs)


async def refresh_destination(*args: object, **kwargs: object) -> None:
    from .telegram_runtime import refresh_destination as impl

    return await impl(*args, **kwargs)


async def chat_title(*args: object, **kwargs: object) -> str:
    from .telegram_runtime import chat_title as impl

    return await impl(*args, **kwargs)


async def source_permalink(*args: object, **kwargs: object) -> str | None:
    from .telegram_runtime import source_permalink as impl

    return await impl(*args, **kwargs)


def matches_any(*args: object, **kwargs: object) -> bool:
    from .telegram_runtime import matches_any as impl

    return impl(*args, **kwargs)



def destination_peer_id(*args: object, **kwargs: object) -> str:
    from .telegram_runtime import destination_peer_id as impl

    return impl(*args, **kwargs)


def deleted_event_source_ids(*args: object, **kwargs: object) -> tuple[str, ...]:
    from .telegram_runtime import deleted_event_source_ids as impl

    return impl(*args, **kwargs)


def message_source_ids(*args: object, **kwargs: object) -> tuple[str, ...]:
    from .telegram_runtime import message_source_ids as impl

    return impl(*args, **kwargs)


def preferred_source_id(*args: object, **kwargs: object) -> str:
    from .telegram_runtime import preferred_source_id as impl

    return impl(*args, **kwargs)


def mark_seen_message(*args: object, **kwargs: object) -> None:
    from .telegram_runtime import mark_seen_message as impl

    return impl(*args, **kwargs)


def bot_api_token(*args: object, **kwargs: object) -> str | None:
    from .delivery import bot_api_token as impl

    return impl(*args, **kwargs)


def is_private_user_destination(*args: object, **kwargs: object) -> bool:
    from .delivery import is_private_user_destination as impl

    return impl(*args, **kwargs)


def bot_api_request_sync(*args: object, **kwargs: object) -> dict[str, object]:
    from .delivery import bot_api_request_sync as impl

    return impl(*args, **kwargs)


async def bot_api_request(*args: object, **kwargs: object) -> dict[str, object]:
    from .delivery import bot_api_request as impl

    return await impl(*args, **kwargs)


def bot_api_not_modified(*args: object, **kwargs: object) -> bool:
    from .delivery import bot_api_not_modified as impl

    return impl(*args, **kwargs)


def bot_api_message_already_absent(*args: object, **kwargs: object) -> bool:
    from .delivery import bot_api_message_already_absent as impl

    return impl(*args, **kwargs)


async def bot_api_edit_message(*args: object, **kwargs: object) -> bool:
    from .delivery import bot_api_edit_message as impl

    return await impl(*args, **kwargs)


async def bot_api_delete_messages(*args: object, **kwargs: object) -> bool:
    from .delivery import bot_api_delete_messages as impl

    return await impl(*args, **kwargs)


async def resolve_offer_urls(urls: Iterable[str]) -> tuple[str, ...]:
    resolved = []
    for url in urls:
        final_url = await asyncio.to_thread(resolve_redirect_url, url)
        for candidate in (final_url, url):
            if candidate and candidate not in resolved:
                resolved.append(candidate)
    return tuple(resolved)


def message_offer_urls(message: Message) -> tuple[str, ...]:
    urls = []
    for url in re.findall(r"https?://[^\s<>()\"']+", message.raw_text or ""):
        urls.append(url.rstrip(".,;:!?]})"))

    for entity in getattr(message, "entities", None) or []:
        url = getattr(entity, "url", None)
        if url:
            urls.append(str(url))

    for row in getattr(message, "buttons", None) or []:
        for button in row:
            url = getattr(button, "url", None)
            if url:
                urls.append(str(url))

    deduped = []
    for url in urls:
        if url not in deduped:
            deduped.append(url)
    return tuple(deduped)


async def category_context_for_offer_urls(urls: Iterable[str]) -> str:
    url_tuple = tuple(url for url in urls if url)
    if not url_tuple:
        return ""
    return await asyncio.to_thread(combined_site_context, url_tuple)


async def offer_activity_for_offer_urls(
    urls: Iterable[str],
    expected_price: Decimal | None = None,
) -> OfferActivity:
    last_activity: OfferActivity | None = None
    for url in tuple(url for url in urls if url)[:3]:
        activity = await asyncio.to_thread(offer_activity_for_url, url, expected_price)
        if activity.status in {"active", "inactive"}:
            return activity
        last_activity = activity
    return last_activity or OfferActivity(url="", status="unknown", reason="no-url", fetched=False)



async def edit_or_replace_offer_with_gif(
    senders: Iterable[TelegramClient],
    destination: object,
    store: DedupeStore,
    offer: OfferRecord,
    gif_path: Path,
    caption: str,
    target_has_media: bool,
) -> bool:
    senders_tuple = tuple(senders)
    if target_has_media:
        for sender in senders_tuple:
            try:
                await asyncio.wait_for(
                    sender.edit_message(
                        destination,
                        offer.primary_message_id,
                        caption,
                        file=gif_path,
                        parse_mode=None,
                    ),
                    timeout=PRODUCT_GIF_UPLOAD_TIMEOUT_SECONDS,
                )
                return True
            except Exception as exc:
                LOGGER.warning(
                    "Could not edit offer %s media GIF with %s: %s",
                    offer.fingerprint,
                    sender.session.__class__.__name__,
                    exc,
                )

    for sender in senders_tuple:
        try:
            result = await asyncio.wait_for(
                sender.send_file(
                    destination,
                    file=gif_path,
                    caption=caption,
                    parse_mode=None,
                ),
                timeout=PRODUCT_GIF_UPLOAD_TIMEOUT_SECONDS,
            )
            message_ids = _message_ids_from_result(result)
            if not message_ids:
                continue
            old_ids = [offer.primary_message_id, *offer.extra_message_ids]
            store.update_offer_delivery(
                offer.fingerprint,
                message_ids[0],
                tuple(message_ids[1:]),
            )
            await delete_messages_with_fallback(
                senders_tuple,
                destination,
                old_ids,
                offer=offer,
            )
            return True
        except Exception as exc:
            LOGGER.warning(
                "Could not replace offer %s with GIF via %s: %s",
                offer.fingerprint,
                sender.session.__class__.__name__,
                exc,
            )
    return False


async def refresh_offer_media_gif(
    *,
    senders: Iterable[TelegramClient],
    source_reader: TelegramClient,
    destination: object,
    store: DedupeStore,
    offer: OfferRecord,
    max_chars: int,
    current_source_message: Message | None = None,
) -> bool:
    sources = store.offer_source_messages(offer.fingerprint)
    if len(sources) < 2 and current_source_message is None:
        return False

    caption = trim_text(
        build_offer_publish_text_from_body(
            body=offer.text,
            category=offer.category,
            sources=store.offer_sources(offer.fingerprint),
            max_chars=max_chars,
        ),
        CAPTION_LIMIT,
    )
    senders_tuple = tuple(senders)
    with tempfile.TemporaryDirectory(prefix="shopperbot-gif-") as temp_dir:
        temp_path = Path(temp_dir)
        media_paths: list[Path] = []
        seen_source_messages: set[tuple[str, int]] = set()

        target_message = await get_destination_message_with_media(
            senders_tuple,
            destination,
            offer.primary_message_id,
        )
        target_has_media = target_message is not None
        if target_message is not None:
            target_dir = temp_path / "target"
            target_dir.mkdir()
            target_media = await download_message_media(target_message, target_dir)
            if target_media is not None:
                media_paths.append(target_media)

        if current_source_message is not None and getattr(current_source_message, "media", None):
            current_source_ids = message_source_ids(current_source_message)
            current_source_id = current_source_ids[0] if current_source_ids else ""
            seen_source_messages.add((current_source_id, int(current_source_message.id)))
            current_dir = temp_path / "current"
            current_dir.mkdir()
            current_media = await download_message_media(current_source_message, current_dir)
            if current_media is not None:
                media_paths.append(current_media)

        for source in sources:
            source_key = (source.source_chat_id, source.source_message_id)
            if source_key in seen_source_messages:
                continue
            source_message = await get_source_message(source_reader, source)
            if source_message is None or not getattr(source_message, "media", None):
                continue
            source_dir = temp_path / f"source-{len(media_paths)}"
            source_dir.mkdir()
            source_media = await download_message_media(source_message, source_dir)
            if source_media is not None:
                media_paths.append(source_media)

        gif_path = temp_path / "product-images.gif"
        if not create_product_gif(media_paths, gif_path):
            return False

        return await edit_or_replace_offer_with_gif(
            senders_tuple,
            destination,
            store,
            offer,
            gif_path,
            caption,
            target_has_media,
        )



async def edit_offer_message(*args: object, **kwargs: object) -> bool:
    from .delivery import edit_offer_message as impl

    return await impl(*args, **kwargs)


async def send_menu_message(*args: object, **kwargs: object) -> list[int]:
    from .menu_runtime import send_menu_message as impl

    return await impl(*args, **kwargs)


async def edit_menu_message(*args: object, **kwargs: object) -> bool:
    from .menu_runtime import edit_menu_message as impl

    return await impl(*args, **kwargs)


async def upsert_offer_menu(*args: object, **kwargs: object) -> bool:
    from .menu_runtime import upsert_offer_menu as impl

    return await impl(*args, **kwargs)


async def upsert_menu_index(*args: object, **kwargs: object) -> bool:
    from .menu_runtime import upsert_menu_index as impl

    return await impl(*args, **kwargs)


async def delete_legacy_offer_menus(*args: object, **kwargs: object) -> tuple[int, int]:
    from .menu_runtime import delete_legacy_offer_menus as impl

    return await impl(*args, **kwargs)


async def delete_offer_menu(*args: object, **kwargs: object) -> bool:
    from .menu_runtime import delete_offer_menu as impl

    return await impl(*args, **kwargs)


async def delete_open_menu_expansions(*args: object, **kwargs: object) -> tuple[int, int]:
    from .menu_runtime import delete_open_menu_expansions as impl

    return await impl(*args, **kwargs)


def build_offer_detail_text(*args: object, **kwargs: object) -> str:
    from .menu_runtime import build_offer_detail_text as impl

    return impl(*args, **kwargs)


async def collect_offer_media_paths(*args: object, **kwargs: object) -> list[Path]:
    from .menu_runtime import collect_offer_media_paths as impl

    return await impl(*args, **kwargs)


async def send_offer_detail_message(*args: object, **kwargs: object) -> list[int]:
    from .menu_runtime import send_offer_detail_message as impl

    return await impl(*args, **kwargs)


async def expand_offer_menu(*args: object, **kwargs: object) -> tuple[int, int]:
    from .menu_runtime import expand_offer_menu as impl

    return await impl(*args, **kwargs)


async def sync_offer_menus(*args: object, **kwargs: object) -> tuple[int, int, int]:
    from .menu_runtime import sync_offer_menus as impl

    return await impl(*args, **kwargs)


async def sync_offer_menu_for_offer(*args: object, **kwargs: object) -> tuple[int, int, int]:
    from .menu_runtime import sync_offer_menu_for_offer as impl

    return await impl(*args, **kwargs)


async def migrate_active_posts_to_menu_only(*args: object, **kwargs: object) -> tuple[int, int, int, int]:
    from .menu_runtime import migrate_active_posts_to_menu_only as impl

    return await impl(*args, **kwargs)


async def delete_messages_with_fallback(*args: object, **kwargs: object) -> bool:
    from .delivery import delete_messages_with_fallback as impl

    return await impl(*args, **kwargs)


async def delete_offer(*args: object, **kwargs: object) -> bool:
    from .delivery import delete_offer as impl

    return await impl(*args, **kwargs)


async def canonical_fingerprint_for_offer(*args: object, **kwargs: object) -> str | None:
    from .maintenance import canonical_fingerprint_for_offer as impl

    return await impl(*args, **kwargs)


async def merge_offer_record_into(*args: object, **kwargs: object) -> bool:
    from .maintenance import merge_offer_record_into as impl

    return await impl(*args, **kwargs)


async def merge_duplicate_active_offers(*args: object, **kwargs: object) -> tuple[int, int, int]:
    from .maintenance import merge_duplicate_active_offers as impl

    return await impl(*args, **kwargs)


async def refresh_active_offer_gifs(*args: object, **kwargs: object) -> tuple[int, int, int, int]:
    from .maintenance import refresh_active_offer_gifs as impl

    return await impl(*args, **kwargs)


async def purge_filtered_offers(*args: object, **kwargs: object) -> tuple[int, int]:
    from .maintenance import purge_filtered_offers as impl

    return await impl(*args, **kwargs)


async def purge_inactive_link_offers(*args: object, **kwargs: object) -> tuple[int, int, int, int, int]:
    from .maintenance import purge_inactive_link_offers as impl

    return await impl(*args, **kwargs)


async def purge_inactive_published_messages(*args: object, **kwargs: object) -> tuple[int, int, int, int, int]:
    from .maintenance import purge_inactive_published_messages as impl

    return await impl(*args, **kwargs)


async def purge_legacy_offers(*args: object, **kwargs: object) -> tuple[int, int]:
    from .maintenance import purge_legacy_offers as impl

    return await impl(*args, **kwargs)


async def verify_marked_deleted_offers(*args: object, **kwargs: object) -> tuple[int, int]:
    from .maintenance import verify_marked_deleted_offers as impl

    return await impl(*args, **kwargs)


async def reformat_active_offers(*args: object, **kwargs: object) -> tuple[int, int]:
    from .maintenance import reformat_active_offers as impl

    return await impl(*args, **kwargs)


async def recategorize_active_offers(*args: object, **kwargs: object) -> tuple[int, int, int, int]:
    from .maintenance import recategorize_active_offers as impl

    return await impl(*args, **kwargs)


async def parse_published_offer_message(message: Message) -> PublishedOfferMessage | None:
    text = message.raw_text or ""
    if not is_structured_offer_text(text):
        return None
    urls = await resolve_offer_urls(re.findall(r"https?://[^\s<>()\"']+", text))
    if not urls:
        return None
    product = extract_offer_label(text, "Prodotto:")
    original = extract_offer_label(text, "Prezzo originale:")
    current = extract_offer_label(text, "Prezzo attuale:")
    category = extract_offer_label(text, "Categoria:")
    return PublishedOfferMessage(
        message_id=int(message.id),
        fingerprint=fingerprint_for_offer_url(urls[0]),
        urls=urls,
        product=product,
        original_price=parse_price_limit(original.lower()) if original else None,
        current_price=parse_price_limit(current.lower()) if current else None,
        category=category.lower() if category else None,
    )


def published_offers_match(left: PublishedOfferMessage, right: PublishedOfferMessage) -> bool:
    if left.fingerprint == right.fingerprint:
        return True
    if (
        left.product
        and right.product
        and left.original_price is not None
        and left.current_price is not None
        and left.original_price == right.original_price
        and left.current_price == right.current_price
    ):
        return product_similarity(left.product, right.product) >= 0.78
    return False


def parse_published_offer_message_fast(message_id: int, text: str) -> PublishedOfferMessage | None:
    if not is_structured_offer_text(text):
        return None
    urls = re.findall(r"https?://[^\s<>()\"']+", text)
    if not urls:
        return None
    product = extract_offer_label(text, "Prodotto:")
    original = extract_offer_label(text, "Prezzo originale:")
    current = extract_offer_label(text, "Prezzo attuale:")
    category = extract_offer_label(text, "Categoria:")
    return PublishedOfferMessage(
        message_id=message_id,
        fingerprint=fingerprint_for_offer_url(urls[0]),
        urls=tuple(urls),
        product=product,
        original_price=parse_price_limit(original.lower()) if original else None,
        current_price=parse_price_limit(current.lower()) if current else None,
        category=category.lower() if category else None,
    )


def offer_record_as_published(offer: OfferRecord) -> PublishedOfferMessage:
    return PublishedOfferMessage(
        message_id=offer.primary_message_id,
        fingerprint=offer.fingerprint,
        urls=offer_record_urls(offer),
        product=offer_record_product(offer),
        original_price=offer_record_original_price(offer),
        current_price=offer.price,
        category=offer.category,
    )


async def first_user_client(senders: Iterable[TelegramClient]) -> TelegramClient | None:
    for sender in senders:
        try:
            me = await sender.get_me()
        except Exception as exc:
            LOGGER.debug("Could not identify Telegram client for private cleanup: %s", exc)
            continue
        if not getattr(me, "bot", False):
            return sender
    return None


async def bot_api_get_me(*args: object, **kwargs: object) -> dict[str, object] | None:
    from .private_history import bot_api_get_me as impl

    return await impl(*args, **kwargs)


async def detect_private_message_id_offset(*args: object, **kwargs: object) -> int | None:
    from .private_history import detect_private_message_id_offset as impl

    return await impl(*args, **kwargs)


async def load_private_dialog_cache(*args: object, **kwargs: object):
    from .private_history import load_private_dialog_cache as impl

    return await impl(*args, **kwargs)


async def delete_private_bot_dialog_messages(*args: object, **kwargs: object) -> bool:
    from .private_history import delete_private_bot_dialog_messages as impl

    return await impl(*args, **kwargs)


async def purge_inactive_private_published_messages(*args: object, **kwargs: object) -> tuple[int, int, int, int, int]:
    from .private_history import purge_inactive_private_published_messages as impl

    return await impl(*args, **kwargs)


async def purge_unmerged_private_bot_dialog_messages(*args: object, **kwargs: object) -> tuple[int, int, int]:
    from .private_history import purge_unmerged_private_bot_dialog_messages as impl

    return await impl(*args, **kwargs)


def message_passes_history_filters(*args: object, **kwargs: object) -> bool:
    from .private_history import message_passes_history_filters as impl

    return impl(*args, **kwargs)


async def purge_private_structured_offer_messages(*args: object, **kwargs: object) -> tuple[int, int, int]:
    from .private_history import purge_private_structured_offer_messages as impl

    return await impl(*args, **kwargs)


async def purge_private_history_messages(*args: object, **kwargs: object) -> tuple[int, int, int, int, int, int]:
    from .private_history import purge_private_history_messages as impl

    return await impl(*args, **kwargs)


async def purge_unmerged_destination_messages(*args: object, **kwargs: object) -> tuple[int, int, int]:
    from .maintenance import purge_unmerged_destination_messages as impl

    return await impl(*args, **kwargs)


async def sync_menus_after_offer_changes(*args: object, **kwargs: object) -> None:
    from .message_processing import sync_menus_after_offer_changes as impl

    return await impl(*args, **kwargs)


async def delete_offer_fingerprints(*args: object, **kwargs: object) -> bool:
    from .message_processing import delete_offer_fingerprints as impl

    return await impl(*args, **kwargs)


async def merge_existing_offer_source(*args: object, **kwargs: object) -> None:
    from .message_processing import merge_existing_offer_source as impl

    return await impl(*args, **kwargs)


async def publish_new_offer(*args: object, **kwargs: object) -> None:
    from .message_processing import publish_new_offer as impl

    return await impl(*args, **kwargs)


def mapped_offer_fingerprints(*args: object, **kwargs: object) -> set[str]:
    from .message_processing import mapped_offer_fingerprints as impl

    return impl(*args, **kwargs)


def message_was_seen(*args: object, **kwargs: object) -> bool:
    from .message_processing import message_was_seen as impl

    return impl(*args, **kwargs)


def message_matches_text_rules(*args: object, **kwargs: object) -> bool:
    from .message_processing import message_matches_text_rules as impl

    return impl(*args, **kwargs)


async def reject_invalid_or_incomplete_offer(*args: object, **kwargs: object) -> bool:
    from .message_processing import reject_invalid_or_incomplete_offer as impl

    return await impl(*args, **kwargs)


async def reject_filtered_offer(*args: object, **kwargs: object) -> bool:
    from .message_processing import reject_filtered_offer as impl

    return await impl(*args, **kwargs)


async def process_complete_offer(*args: object, **kwargs: object) -> None:
    from .message_processing import process_complete_offer as impl

    return await impl(*args, **kwargs)


async def handle_message(*args: object, **kwargs: object) -> None:
    from .message_processing import handle_message as impl

    return await impl(*args, **kwargs)


async def run_backfill(
    *,
    client: TelegramClient,
    sources: list[object],
    settings: Settings,
    store: DedupeStore,
    state: RuntimeState,
    sender: TelegramClient,
    cleanup_senders: tuple[TelegramClient, ...],
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
                state=state,
                sender=sender,
                cleanup_senders=cleanup_senders,
                destination=destination,
                limiter=limiter,
                ignored_chat_ids=ignored_chat_ids,
            )


async def run_expired_offer_checks(
    *,
    reader: TelegramClient,
    senders: Iterable[TelegramClient],
    store: DedupeStore,
    state: RuntimeState,
    settings: Settings,
) -> None:
    if settings.expired_offer_check_interval_seconds <= 0:
        return

    await asyncio.sleep(min(60, settings.expired_offer_check_interval_seconds))
    while True:
        if state.destination is None:
            await asyncio.sleep(settings.expired_offer_check_interval_seconds)
            continue
        try:
            tracked_scanned, tracked_deleted, tracked_active, tracked_unknown, tracked_failed = await purge_inactive_link_offers(
                senders=senders,
                destination=state.destination,
                store=store,
                limit=settings.expired_offer_check_limit,
            )
            history_scanned, history_deleted, history_active, history_unknown, history_failed = await purge_inactive_published_messages(
                reader=reader,
                senders=senders,
                destination=state.destination,
                store=store,
                limit=settings.expired_offer_check_limit,
            )
            LOGGER.info(
                "Expired offer link check: tracked_scanned=%s history_scanned=%s "
                "deleted=%s active=%s unknown=%s failed=%s",
                tracked_scanned,
                history_scanned,
                tracked_deleted + history_deleted,
                tracked_active + history_active,
                tracked_unknown + history_unknown,
                tracked_failed + history_failed,
            )
            if (
                is_menu_only_enabled(store)
                and tracked_deleted + history_deleted > 0
                and state.destination is not None
            ):
                await sync_offer_menus(
                    senders=senders,
                    destination=state.destination,
                    store=store,
                    max_chars=settings.max_text_chars,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Could not complete expired offer link check")
        await asyncio.sleep(settings.expired_offer_check_interval_seconds)


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
        "/filters - mostra filtri categoria/prezzo",
        "/category <nomi|clear|list> - filtra per categoria",
        "/price <min|max|clear> [valore] - filtra per prezzo",
        "/publish_mode <posts|menu> - cambia tra post singoli e menu-only",
        "/menu_sync - ricostruisce/aggiorna tutti i menu",
        "/scan_sources [offerte|notizie] - aggiunge bot, canali e gruppi compatibili",
        "/scan_bots [offerte|notizie] - alias che scansiona tutte le sorgenti",
        "/purge_legacy - elimina offerte pubblicate col vecchio formato",
        "/purge_legacy hard - ritenta anche vecchie eliminazioni fallite",
        "/purge_deleted - ritenta le cancellazioni gia' segnate nel database",
        "/purge_expired [numero|all] - controlla i link e elimina offerte terminate",
        "/purge_unmerged [numero] - elimina duplicati vecchi non registrati nel DB",
        "/purge_product_posts [numero|all] - elimina i vecchi messaggi prodotto e lascia il menu",
        "/purge_history [numero] [keep=N] - ripulisce la chat privata da offerte vecchie/duplicate/fuori filtro",
        "/recategorize [numero|all] - ricalcola categorie usando il sito dell'offerta",
        "/refresh_gifs [numero|all] - crea GIF per offerte gia' unite con immagini diverse",
        "/reconcile - sincronizza filtri, merge e formato dei messaggi gia' pubblicati",
        "/diagnose_destination - prova invio, modifica e cancellazione nella destinazione",
        "/sources - mostra le sorgenti attive",
        "/clear_sources - svuota le sorgenti",
        "/status - stato del servizio",
        "/whoami - mostra user id e chat id",
    ]
    return "\n".join(lines)


def filters_text(store: DedupeStore) -> str:
    categories = tuple(item for item in store.get_filter_categories() if item != "altro")
    min_price = store.get_filter_price("filter_min_price")
    max_price = store.get_filter_price("filter_max_price")
    return "\n".join(
        [
            "Filtri attivi",
            f"Categorie: {', '.join(categories) if categories else 'tutte'}",
            f"Prezzo minimo: {min_price if min_price is not None else 'nessuno'}",
            f"Prezzo massimo: {max_price if max_price is not None else 'nessuno'}",
            "",
            "Categorie disponibili:",
            ", ".join(known_filter_categories()),
            "Nota: filtrare per 'libri' include anche sottocategorie come libri/thriller.",
        ]
    )


def unique_clients(*clients: TelegramClient) -> tuple[TelegramClient, ...]:
    unique = []
    for client in clients:
        if not any(client is existing for existing in unique):
            unique.append(client)
    return tuple(unique)


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
    from .control_bot import register_control_bot as register_handlers

    await register_handlers(
        bot=bot,
        source_client=source_client,
        settings=settings,
        store=store,
        state=state,
    )


async def start_control_sender(settings: Settings) -> TelegramClient | None:
    if not settings.telegram_bot_token:
        return None

    bot_session_path = settings.database_path.parent / "control_bot"
    sender_client = TelegramClient(
        str(bot_session_path),
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    while True:
        try:
            await sender_client.start(bot_token=settings.telegram_bot_token)
            return sender_client
        except FloodWaitError as exc:
            LOGGER.warning(
                "Telegram asked to wait %s seconds before bot authorization; sleeping",
                exc.seconds,
            )
            await asyncio.sleep(exc.seconds + 5)


async def load_runtime_configuration(
    *,
    source_client: TelegramClient,
    sender: TelegramClient,
    settings: Settings,
    store: DedupeStore,
    state: RuntimeState,
) -> None:
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


async def setup_control_bot(
    *,
    sender_client: TelegramClient | None,
    source_client: TelegramClient,
    settings: Settings,
    store: DedupeStore,
    state: RuntimeState,
) -> None:
    if sender_client is None:
        return

    bot_self = await sender_client.get_me()
    state.control_bot_peer_id = entity_peer_id(bot_self)
    state.ignored_chat_ids.add(state.control_bot_peer_id)
    store.remove_source(state.control_bot_peer_id)
    state.source_ids.discard(state.control_bot_peer_id)
    LOGGER.info("Excluding control bot from sources: %s", state.control_bot_peer_id)
    await register_control_bot(
        bot=sender_client,
        source_client=source_client,
        settings=settings,
        store=store,
        state=state,
    )


async def run_startup_maintenance(
    *,
    cleanup_senders: tuple[TelegramClient, ...],
    settings: Settings,
    store: DedupeStore,
    state: RuntimeState,
) -> None:
    if state.destination is None:
        return
    if not is_menu_only_enabled(store) or settings.dry_run:
        LOGGER.info("Startup maintenance skipped; use /reconcile or /purge_expired to run cleanup")
        return

    menu_updated, menu_deleted, menu_failed = await sync_offer_menus(
        senders=cleanup_senders,
        destination=state.destination,
        store=store,
        max_chars=settings.max_text_chars,
    )
    legacy_scanned = legacy_deleted = legacy_failed = 0
    if is_private_user_destination(state.destination):
        legacy_scanned, legacy_deleted, legacy_failed = await purge_private_structured_offer_messages(
            senders=cleanup_senders,
            destination=state.destination,
            limit=PRIVATE_DELETE_SCAN_LIMIT,
        )
    LOGGER.info(
        "Startup menu-only maintenance: menu_updated=%s menu_deleted=%s "
        "product_scanned=%s product_deleted=%s failed=%s",
        menu_updated,
        menu_deleted,
        legacy_scanned,
        legacy_deleted,
        menu_failed + legacy_failed,
    )


def should_process_source_event(
    settings: Settings,
    state: RuntimeState,
    source_ids: tuple[str, ...],
) -> bool:
    if not source_ids:
        return False
    return settings.monitor_all_chats or any(source_id in state.source_ids for source_id in source_ids)


def register_source_event_handlers(
    *,
    source_client: TelegramClient,
    sender: TelegramClient,
    cleanup_senders: tuple[TelegramClient, ...],
    settings: Settings,
    store: DedupeStore,
    state: RuntimeState,
    limiter: RateLimiter,
) -> None:
    @source_client.on(events.NewMessage(incoming=True))
    async def on_new_message(new_message_event: events.NewMessage.Event) -> None:
        try:
            source_ids = message_source_ids(new_message_event.message)
            if not should_process_source_event(settings, state, source_ids):
                return
            await handle_message(
                message=new_message_event.message,
                settings=settings,
                store=store,
                state=state,
                sender=sender,
                cleanup_senders=cleanup_senders,
                destination=state.destination,
                limiter=limiter,
                ignored_chat_ids=state.ignored_chat_ids,
            )
        except Exception:
            LOGGER.exception("Could not process incoming message")

    @source_client.on(events.MessageEdited(incoming=True))
    async def on_edited_message(edited_event: events.MessageEdited.Event) -> None:
        try:
            source_ids = message_source_ids(edited_event.message)
            if not should_process_source_event(settings, state, source_ids):
                return
            await handle_message(
                message=edited_event.message,
                settings=settings,
                store=store,
                state=state,
                sender=sender,
                cleanup_senders=cleanup_senders,
                destination=state.destination,
                limiter=limiter,
                ignored_chat_ids=state.ignored_chat_ids,
                allow_seen_update=True,
            )
        except Exception:
            LOGGER.exception("Could not process edited message")

    @source_client.on(events.MessageDeleted())
    async def on_deleted_message(deleted_event: events.MessageDeleted.Event) -> None:
        try:
            source_ids = deleted_event_source_ids(deleted_event.chat_id)
            if not should_process_source_event(settings, state, source_ids):
                return
            for deleted_id in deleted_event.deleted_ids:
                deleted_fingerprints = mapped_offer_fingerprints(store, source_ids, int(deleted_id))
                await delete_offer_fingerprints(
                    cleanup_senders=cleanup_senders,
                    destination=state.destination,
                    store=store,
                    settings=settings,
                    fingerprints=deleted_fingerprints,
                    reason="source-deleted",
                )
        except Exception:
            LOGGER.exception("Could not process deleted message")


async def stop_runtime(
    *,
    store: DedupeStore,
    source_client: TelegramClient,
    sender_client: TelegramClient | None,
    expired_checks_task: asyncio.Task[None] | None,
) -> None:
    if expired_checks_task is not None:
        expired_checks_task.cancel()
        try:
            await expired_checks_task
        except asyncio.CancelledError:
            pass
    store.close()
    await source_client.disconnect()
    if sender_client is not None:
        await sender_client.disconnect()


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
    expired_checks_task: asyncio.Task[None] | None = None

    try:
        await source_client.start()
        state = RuntimeState()
        sender_client = await start_control_sender(settings)
        sender = sender_client or source_client
        await load_runtime_configuration(
            source_client=source_client,
            sender=sender,
            settings=settings,
            store=store,
            state=state,
        )
        await setup_control_bot(
            sender_client=sender_client,
            source_client=source_client,
            settings=settings,
            store=store,
            state=state,
        )

        cleanup_senders = unique_clients(sender, source_client)
        await run_startup_maintenance(
            cleanup_senders=cleanup_senders,
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
            state=state,
            sender=sender,
            cleanup_senders=cleanup_senders,
            destination=state.destination,
            limiter=limiter,
            ignored_chat_ids=state.ignored_chat_ids,
        )
        register_source_event_handlers(
            source_client=source_client,
            sender=sender,
            cleanup_senders=cleanup_senders,
            settings=settings,
            store=store,
            state=state,
            limiter=limiter,
        )
        expired_checks_task = asyncio.create_task(
            run_expired_offer_checks(
                reader=sender,
                senders=cleanup_senders,
                store=store,
                state=state,
                settings=settings,
            )
        )
        LOGGER.info("Shopper Easly bot is online")
        await source_client.run_until_disconnected()
    finally:
        await stop_runtime(
            store=store,
            source_client=source_client,
            sender_client=sender_client,
            expired_checks_task=expired_checks_task,
        )


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
