from __future__ import annotations

import logging
from typing import Iterable

from telethon import TelegramClient
from telethon.tl.types import Message

from .config import Settings
from .dedupe import DedupeStore, OfferRecord
from .filters import passes_filters
from .menu import is_menu_only_enabled
from .offer_analysis import analyze_offer
from .offers import (
    build_offer_publish_text,
    build_offer_publish_text_from_body,
    find_similar_active_offer,
    fingerprint_for_offer_url,
    fingerprints_from_urls,
    stable_offer_body,
)
from .runtime import (
    RateLimiter,
    RuntimeState,
    category_context_for_offer_urls,
    chat_title,
    delete_offer,
    destination_peer_id,
    edit_offer_message,
    first_user_client,
    mark_seen_message,
    matches_any,
    message_offer_urls,
    message_source_ids,
    preferred_source_id,
    refresh_offer_media_gif,
    resolve_offer_urls,
    send_with_retry,
    source_permalink,
    sync_offer_menu_for_offer,
    sync_offer_menus,
)


LOGGER = logging.getLogger("shopper_merge_bot")


async def sync_menus_after_offer_changes(
    *,
    changed: bool,
    settings: Settings,
    store: DedupeStore,
    cleanup_senders: tuple[TelegramClient, ...],
    destination: object | None,
) -> None:
    if not changed or not is_menu_only_enabled(store) or settings.dry_run:
        return
    await sync_offer_menus(
        senders=cleanup_senders,
        destination=destination,
        store=store,
        max_chars=settings.max_text_chars,
    )

async def delete_offer_fingerprints(
    *,
    cleanup_senders: tuple[TelegramClient, ...],
    destination: object | None,
    store: DedupeStore,
    settings: Settings,
    fingerprints: Iterable[str],
    reason: str,
) -> bool:
    deleted_any = False
    for fingerprint in tuple(dict.fromkeys(item for item in fingerprints if item)):
        deleted_any = await delete_offer(
            cleanup_senders,
            destination,
            store,
            fingerprint,
            reason,
        ) or deleted_any
    await sync_menus_after_offer_changes(
        changed=deleted_any,
        settings=settings,
        store=store,
        cleanup_senders=cleanup_senders,
        destination=destination,
    )
    return deleted_any

async def merge_existing_offer_source(
    *,
    existing: OfferRecord,
    facts: object,
    message: Message,
    source_ids: tuple[str, ...],
    source_id: str,
    message_id: int,
    title: str,
    source_link: str | None,
    settings: Settings,
    store: DedupeStore,
    sender: TelegramClient,
    cleanup_senders: tuple[TelegramClient, ...],
    destination: object,
) -> None:
    target_fingerprint = existing.fingerprint
    added = store.add_offer_source(
        fingerprint=target_fingerprint,
        source_chat_id=source_id,
        source_message_id=message_id,
        source_title=title,
        source_link=source_link or "",
    )
    mark_seen_message(store, source_ids, message_id)

    if existing.category == "altro" and facts.category != "altro":
        store.update_offer_category(target_fingerprint, facts.category)
        existing = store.get_offer(target_fingerprint) or existing

    if not added:
        return

    sources = store.offer_sources(target_fingerprint)
    merged = build_offer_publish_text_from_body(
        body=existing.text,
        category=existing.category,
        sources=sources,
        max_chars=settings.max_text_chars,
    )
    store.update_offer_text(target_fingerprint, existing.text, len(sources))
    existing = store.get_offer(target_fingerprint) or existing

    if not settings.dry_run:
        if is_menu_only_enabled(store):
            await sync_offer_menu_for_offer(
                senders=cleanup_senders,
                destination=destination,
                store=store,
                offer=existing,
                max_chars=settings.max_text_chars,
            )
        else:
            media_updated = False
            if settings.copy_media and message.media:
                source_reader = await first_user_client(cleanup_senders) or sender
                media_updated = await refresh_offer_media_gif(
                    senders=cleanup_senders,
                    source_reader=source_reader,
                    destination=destination,
                    store=store,
                    offer=existing,
                    max_chars=settings.max_text_chars,
                    current_source_message=message,
                )
            if not media_updated:
                await edit_offer_message(cleanup_senders, destination, existing, merged)

    LOGGER.info("Merged duplicate offer %s from %s/%s", target_fingerprint, source_id, message_id)

async def publish_new_offer(
    *,
    message: Message,
    source_ids: tuple[str, ...],
    source_id: str,
    message_id: int,
    title: str,
    source_link: str | None,
    fingerprint: str,
    normalized_body: str,
    facts: object,
    settings: Settings,
    store: DedupeStore,
    sender: TelegramClient,
    cleanup_senders: tuple[TelegramClient, ...],
    destination: object,
    limiter: RateLimiter,
) -> None:
    outbound = build_offer_publish_text(
        product=str(facts.product),
        original_price=facts.original_price,
        current_price=facts.current_price,
        offer_url=str(facts.offer_url),
        category=facts.category,
        sources=[(title, source_link or "")],
        max_chars=settings.max_text_chars,
    )

    message_ids: list[int] = []
    if settings.dry_run:
        LOGGER.info("DRY_RUN message from %s/%s:\n%s", source_id, message_id, outbound)
    elif is_menu_only_enabled(store):
        message_ids = [0]
    else:
        message_ids = await send_with_retry(
            sender=sender,
            destination=destination,
            message=message,
            text=outbound,
            copy_media=settings.copy_media,
            limiter=limiter,
        )

    if message_ids:
        store.save_offer(
            fingerprint=fingerprint,
            destination_chat_id=destination_peer_id(destination),
            primary_message_id=message_ids[0],
            extra_message_ids=tuple(message_ids[1:]),
            text=normalized_body,
            category=facts.category,
            price=facts.price,
        )
        store.add_offer_source(
            fingerprint=fingerprint,
            source_chat_id=source_id,
            source_message_id=message_id,
            source_title=title,
            source_link=source_link or "",
        )
        if not settings.dry_run and is_menu_only_enabled(store):
            stored_offer = store.get_offer(fingerprint)
            if stored_offer is not None:
                await sync_offer_menu_for_offer(
                    senders=cleanup_senders,
                    destination=destination,
                    store=store,
                    offer=stored_offer,
                    max_chars=settings.max_text_chars,
                )

    mark_seen_message(store, source_ids, message_id)
    LOGGER.info("Delivered message from %s/%s", source_id, message_id)

def mapped_offer_fingerprints(
    store: DedupeStore,
    source_ids: tuple[str, ...],
    message_id: int,
) -> set[str]:
    fingerprints: set[str] = set()
    for candidate_source_id in source_ids:
        fingerprints.update(
            store.fingerprints_for_source_message(candidate_source_id, message_id)
        )
    return fingerprints

def message_was_seen(store: DedupeStore, source_ids: tuple[str, ...], message_id: int) -> bool:
    return any(
        store.has_message(candidate_source_id, message_id)
        for candidate_source_id in source_ids
    )

def message_matches_text_rules(settings: Settings, raw_text: str) -> bool:
    if settings.allow_patterns and not matches_any(raw_text, settings.allow_patterns):
        return False
    if settings.skip_patterns and matches_any(raw_text, settings.skip_patterns):
        return False
    return True

async def reject_invalid_or_incomplete_offer(
    *,
    facts: object,
    offer_urls: tuple[str, ...],
    mapped_fingerprints: set[str],
    source_ids: tuple[str, ...],
    source_id: str,
    message_id: int,
    allow_seen_update: bool,
    settings: Settings,
    store: DedupeStore,
    cleanup_senders: tuple[TelegramClient, ...],
    destination: object,
) -> bool:
    if facts.invalid:
        await delete_offer_fingerprints(
            cleanup_senders=cleanup_senders,
            destination=destination,
            store=store,
            settings=settings,
            fingerprints=mapped_fingerprints | fingerprints_from_urls(offer_urls),
            reason="invalid-source-edit",
        )
        mark_seen_message(store, source_ids, message_id)
        return True

    if facts.complete:
        return False

    if allow_seen_update:
        await delete_offer_fingerprints(
            cleanup_senders=cleanup_senders,
            destination=destination,
            store=store,
            settings=settings,
            fingerprints=mapped_fingerprints,
            reason="incomplete-source-edit",
        )
    mark_seen_message(store, source_ids, message_id)
    LOGGER.info("Incomplete offer skipped from %s/%s", source_id, message_id)
    return True

async def reject_filtered_offer(
    *,
    facts: object,
    mapped_fingerprints: set[str],
    source_ids: tuple[str, ...],
    source_id: str,
    message_id: int,
    allow_seen_update: bool,
    settings: Settings,
    store: DedupeStore,
    cleanup_senders: tuple[TelegramClient, ...],
    destination: object,
) -> bool:
    if passes_filters(store, facts.category, facts.price):
        return False

    if allow_seen_update:
        await delete_offer_fingerprints(
            cleanup_senders=cleanup_senders,
            destination=destination,
            store=store,
            settings=settings,
            fingerprints=mapped_fingerprints,
            reason="source-edited-filtered",
        )
    LOGGER.info(
        "Filtered message from %s/%s category=%s price=%s",
        source_id,
        message_id,
        facts.category,
        facts.price,
    )
    mark_seen_message(store, source_ids, message_id)
    return True

async def process_complete_offer(
    *,
    message: Message,
    facts: object,
    fingerprint: str,
    normalized_body: str,
    source_ids: tuple[str, ...],
    source_id: str,
    message_id: int,
    title: str,
    source_link: str | None,
    settings: Settings,
    store: DedupeStore,
    state: RuntimeState,
    sender: TelegramClient,
    cleanup_senders: tuple[TelegramClient, ...],
    destination: object,
    limiter: RateLimiter,
) -> None:
    async with state.offer_processing_lock:
        existing = store.get_offer(fingerprint)
        if existing is None or existing.status != "active":
            existing = find_similar_active_offer(
                store,
                product=str(facts.product),
                original_price=facts.original_price,
                current_price=facts.current_price,
                excluded_fingerprint=fingerprint,
            )

        if existing and existing.status == "active":
            await merge_existing_offer_source(
                existing=existing,
                facts=facts,
                message=message,
                source_ids=source_ids,
                source_id=source_id,
                message_id=message_id,
                title=title,
                source_link=source_link,
                settings=settings,
                store=store,
                sender=sender,
                cleanup_senders=cleanup_senders,
                destination=destination,
            )
            return

        await publish_new_offer(
            message=message,
            source_ids=source_ids,
            source_id=source_id,
            message_id=message_id,
            title=title,
            source_link=source_link,
            fingerprint=fingerprint,
            normalized_body=normalized_body,
            facts=facts,
            settings=settings,
            store=store,
            sender=sender,
            cleanup_senders=cleanup_senders,
            destination=destination,
            limiter=limiter,
        )

async def handle_message(
    *,
    message: Message,
    settings: Settings,
    store: DedupeStore,
    state: RuntimeState,
    sender: TelegramClient,
    cleanup_senders: tuple[TelegramClient, ...],
    destination: object | None,
    limiter: RateLimiter,
    ignored_chat_ids: set[str],
    allow_seen_update: bool = False,
) -> None:
    source_ids = message_source_ids(message)
    source_id = preferred_source_id(source_ids, state.source_ids)
    message_id = int(message.id)
    if not source_id:
        return
    if any(source in ignored_chat_ids for source in source_ids):
        return
    if getattr(message, "out", False):
        mark_seen_message(store, source_ids, message_id)
        return
    if destination is None:
        LOGGER.info("Skipping %s/%s because no destination is configured", source_id, message_id)
        return

    raw_text = message.raw_text or ""
    if not raw_text and not message.media:
        mark_seen_message(store, source_ids, message_id)
        return

    offer_urls = await resolve_offer_urls(message_offer_urls(message))
    mapped_fingerprints = mapped_offer_fingerprints(store, source_ids, message_id)
    site_category_context = await category_context_for_offer_urls(offer_urls)
    facts = analyze_offer(raw_text, offer_urls, site_text=site_category_context)
    if await reject_invalid_or_incomplete_offer(
        facts=facts,
        offer_urls=offer_urls,
        mapped_fingerprints=mapped_fingerprints,
        source_ids=source_ids,
        source_id=source_id,
        message_id=message_id,
        allow_seen_update=allow_seen_update,
        settings=settings,
        store=store,
        cleanup_senders=cleanup_senders,
        destination=destination,
    ):
        return

    if message_was_seen(store, source_ids, message_id) and not allow_seen_update:
        return

    if not message_matches_text_rules(settings, raw_text):
        mark_seen_message(store, source_ids, message_id)
        return

    source_link = await source_permalink(message) if settings.include_source_link else None
    title = await chat_title(message)
    if await reject_filtered_offer(
        facts=facts,
        mapped_fingerprints=mapped_fingerprints,
        source_ids=source_ids,
        source_id=source_id,
        message_id=message_id,
        allow_seen_update=allow_seen_update,
        settings=settings,
        store=store,
        cleanup_senders=cleanup_senders,
        destination=destination,
    ):
        return

    fingerprint = fingerprint_for_offer_url(str(facts.offer_url))
    normalized_body = stable_offer_body(
        product=str(facts.product),
        original_price=facts.original_price,
        current_price=facts.current_price,
        offer_url=str(facts.offer_url),
    )
    stale_fingerprints = mapped_fingerprints - {fingerprint}
    await delete_offer_fingerprints(
        cleanup_senders=cleanup_senders,
        destination=destination,
        store=store,
        settings=settings,
        fingerprints=stale_fingerprints,
        reason="source-edited-new-offer",
    )

    await process_complete_offer(
        message=message,
        facts=facts,
        fingerprint=fingerprint,
        normalized_body=normalized_body,
        source_ids=source_ids,
        source_id=source_id,
        message_id=message_id,
        title=title,
        source_link=source_link,
        settings=settings,
        store=store,
        state=state,
        sender=sender,
        cleanup_senders=cleanup_senders,
        destination=destination,
        limiter=limiter,
    )
