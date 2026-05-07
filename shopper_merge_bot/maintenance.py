from __future__ import annotations

import logging
from typing import Iterable

from telethon import TelegramClient

from .dedupe import DedupeStore, OfferRecord
from .filters import passes_filters
from .menu import is_menu_only_enabled
from .offer_analysis import classify_category
from .offers import (
    build_offer_publish_text_from_body,
    ensure_offer_body_style,
    fingerprint_for_offer_url,
    is_similar_offer,
    is_structured_offer_text,
    offer_record_original_price,
    offer_record_product,
    offer_record_urls,
)
from . import runtime as runtime_api


LOGGER = logging.getLogger("shopper_merge_bot")
PublishedOfferMessage = runtime_api.PublishedOfferMessage


async def category_context_for_offer_urls(*args: object, **kwargs: object) -> str:
    return await runtime_api.category_context_for_offer_urls(*args, **kwargs)


async def delete_messages_with_fallback(*args: object, **kwargs: object) -> bool:
    return await runtime_api.delete_messages_with_fallback(*args, **kwargs)


async def delete_offer(*args: object, **kwargs: object) -> bool:
    return await runtime_api.delete_offer(*args, **kwargs)


async def edit_offer_message(*args: object, **kwargs: object) -> bool:
    return await runtime_api.edit_offer_message(*args, **kwargs)


def is_private_user_destination(destination: object) -> bool:
    return runtime_api.is_private_user_destination(destination)


async def offer_activity_for_offer_urls(*args: object, **kwargs: object):
    return await runtime_api.offer_activity_for_offer_urls(*args, **kwargs)


async def parse_published_offer_message(*args: object, **kwargs: object):
    return await runtime_api.parse_published_offer_message(*args, **kwargs)


async def purge_inactive_private_published_messages(*args: object, **kwargs: object):
    return await runtime_api.purge_inactive_private_published_messages(*args, **kwargs)


async def purge_unmerged_private_bot_dialog_messages(*args: object, **kwargs: object):
    return await runtime_api.purge_unmerged_private_bot_dialog_messages(*args, **kwargs)


async def refresh_offer_media_gif(*args: object, **kwargs: object) -> bool:
    return await runtime_api.refresh_offer_media_gif(*args, **kwargs)


async def resolve_offer_urls(*args: object, **kwargs: object) -> tuple[str, ...]:
    return await runtime_api.resolve_offer_urls(*args, **kwargs)


async def canonical_fingerprint_for_offer(offer: OfferRecord) -> str | None:
    urls = await resolve_offer_urls(offer_record_urls(offer))
    if not urls:
        return None
    return fingerprint_for_offer_url(urls[0])

async def merge_offer_record_into(
    *,
    senders: Iterable[TelegramClient],
    destination: object,
    store: DedupeStore,
    source: OfferRecord,
    target: OfferRecord,
    max_chars: int,
) -> bool:
    if not await delete_offer(
        senders,
        destination,
        store,
        source.fingerprint,
        "merged-duplicate",
    ):
        return False

    source_count = store.merge_offer_into(source.fingerprint, target.fingerprint)
    category = target.category
    if category == "altro" and source.category != "altro":
        category = source.category
        store.update_offer_category(target.fingerprint, category)
    sources = store.offer_sources(target.fingerprint)
    merged_text = build_offer_publish_text_from_body(
        body=target.text,
        category=category,
        sources=sources,
        max_chars=max_chars,
    )
    store.update_offer_text(target.fingerprint, target.text, source_count)
    await edit_offer_message(senders, destination, target, merged_text)
    return True

async def merge_duplicate_active_offers(
    *,
    senders: Iterable[TelegramClient],
    destination: object | None,
    store: DedupeStore,
    max_chars: int,
) -> tuple[int, int, int]:
    if destination is None:
        return 0, 0, len(store.list_active_offers())

    renamed = 0
    merged = 0
    failed = 0
    for offer in store.list_active_offers():
        canonical_fingerprint = await canonical_fingerprint_for_offer(offer)
        if not canonical_fingerprint or canonical_fingerprint == offer.fingerprint:
            continue

        existing = store.get_offer(canonical_fingerprint)
        if existing is None:
            if store.rename_offer_fingerprint(offer.fingerprint, canonical_fingerprint):
                renamed += 1
            else:
                failed += 1
            continue

        if existing.status != "active":
            failed += 1
            continue

        if not await merge_offer_record_into(
            senders=senders,
            destination=destination,
            store=store,
            source=offer,
            target=existing,
            max_chars=max_chars,
        ):
            failed += 1
            continue

        merged += 1

    active = store.list_active_offers()
    consumed: set[str] = set()
    for index, target in enumerate(active):
        if target.fingerprint in consumed or target.status != "active":
            continue
        for source in active[index + 1 :]:
            if source.fingerprint in consumed or source.status != "active":
                continue
            source_product = offer_record_product(source)
            source_original_price = offer_record_original_price(source)
            if (
                source_product is None
                or source_original_price is None
                or source.price is None
            ):
                continue
            if not is_similar_offer(
                target,
                product=source_product,
                original_price=source_original_price,
                current_price=source.price,
            ):
                continue
            if await merge_offer_record_into(
                senders=senders,
                destination=destination,
                store=store,
                source=source,
                target=target,
                max_chars=max_chars,
            ):
                consumed.add(source.fingerprint)
                merged += 1
            else:
                failed += 1

    return renamed, merged, failed

async def refresh_active_offer_gifs(
    *,
    senders: Iterable[TelegramClient],
    source_reader: TelegramClient,
    destination: object | None,
    store: DedupeStore,
    max_chars: int,
    limit: int,
) -> tuple[int, int, int, int]:
    if destination is None:
        return 0, 0, 0, len(store.list_active_offers())

    scanned = 0
    updated = 0
    skipped = 0
    failed = 0
    for offer in store.list_active_offers():
        if limit > 0 and scanned >= limit:
            break
        if len(store.offer_source_messages(offer.fingerprint)) < 2:
            continue
        scanned += 1
        try:
            if await refresh_offer_media_gif(
                senders=senders,
                source_reader=source_reader,
                destination=destination,
                store=store,
                offer=offer,
                max_chars=max_chars,
            ):
                updated += 1
            else:
                skipped += 1
        except Exception as exc:
            LOGGER.warning("Could not refresh offer GIF %s: %s", offer.fingerprint, exc)
            failed += 1
    return scanned, updated, skipped, failed

async def purge_filtered_offers(
    *,
    senders: Iterable[TelegramClient],
    destination: object | None,
    store: DedupeStore,
) -> tuple[int, int]:
    deleted = 0
    failed = 0
    for offer in store.list_active_offers():
        if passes_filters(store, offer.category, offer.price):
            continue
        if await delete_offer(
            senders,
            destination,
            store,
            offer.fingerprint,
            "filter-changed",
        ):
            deleted += 1
        else:
            failed += 1
    return deleted, failed

async def purge_inactive_link_offers(
    *,
    senders: Iterable[TelegramClient],
    destination: object | None,
    store: DedupeStore,
    limit: int,
) -> tuple[int, int, int, int, int]:
    if destination is None:
        return 0, 0, 0, 0, len(store.list_active_offers())

    scanned = 0
    deleted = 0
    active = 0
    unknown = 0
    failed = 0
    for offer in store.list_active_offers_for_link_check(limit):
        scanned += 1
        urls = await resolve_offer_urls(offer_record_urls(offer))
        if not urls:
            unknown += 1
            store.mark_offer_link_check(offer.fingerprint, "unknown:no-url")
            continue

        try:
            activity = await offer_activity_for_offer_urls(urls, offer.price)
        except Exception as exc:
            LOGGER.warning("Could not check offer link %s: %s", offer.fingerprint, exc)
            failed += 1
            store.mark_offer_link_check(offer.fingerprint, f"unknown:{exc.__class__.__name__}")
            continue

        if activity.status == "inactive":
            store.mark_offer_link_check(offer.fingerprint, f"inactive:{activity.reason}")
            if await delete_offer(
                senders,
                destination,
                store,
                offer.fingerprint,
                f"expired-link:{activity.reason}",
            ):
                deleted += 1
            else:
                failed += 1
            continue

        if activity.status == "active":
            active += 1
        else:
            unknown += 1
        store.mark_offer_link_check(offer.fingerprint, f"{activity.status}:{activity.reason}")

    return scanned, deleted, active, unknown, failed

async def purge_inactive_published_messages(
    *,
    reader: TelegramClient,
    senders: Iterable[TelegramClient],
    destination: object | None,
    store: DedupeStore,
    limit: int,
) -> tuple[int, int, int, int, int]:
    if destination is None:
        return 0, 0, 0, 0, 1
    if is_private_user_destination(destination):
        return await purge_inactive_private_published_messages(
            senders=senders,
            destination=destination,
            store=store,
            limit=limit,
        )

    scanned = 0
    deleted = 0
    active = 0
    unknown = 0
    failed = 0
    delete_ids: list[int] = []
    delete_reasons: dict[int, str] = {}
    deleted_fingerprints: dict[int, str] = {}
    iter_limit = None if limit <= 0 else limit
    try:
        async for message in reader.iter_messages(destination, limit=iter_limit):
            published = await parse_published_offer_message(message)
            if published is None:
                continue
            scanned += 1
            try:
                activity = await offer_activity_for_offer_urls(
                    published.urls,
                    published.current_price,
                )
            except Exception as exc:
                LOGGER.warning("Could not check published offer %s: %s", published.message_id, exc)
                failed += 1
                continue

            if activity.status == "inactive":
                delete_ids.append(published.message_id)
                delete_reasons[published.message_id] = activity.reason
                deleted_fingerprints[published.message_id] = published.fingerprint
            elif activity.status == "active":
                active += 1
            else:
                unknown += 1
    except Exception as exc:
        LOGGER.warning("Could not scan destination history for expired offers: %s", exc)
        return scanned, deleted, active, unknown, failed + 1

    for start in range(0, len(delete_ids), 100):
        chunk = delete_ids[start : start + 100]
        if await delete_messages_with_fallback(senders, destination, chunk):
            deleted += len(chunk)
            for message_id in chunk:
                offer = store.get_offer(deleted_fingerprints[message_id])
                if offer is not None and offer.status == "active":
                    store.mark_offer_status(
                        offer.fingerprint,
                        f"deleted:expired-destination:{delete_reasons[message_id]}",
                    )
        else:
            failed += len(chunk)

    return scanned, deleted, active, unknown, failed

async def purge_legacy_offers(
    *,
    senders: Iterable[TelegramClient],
    destination: object | None,
    store: DedupeStore,
    include_marked_deleted: bool = False,
) -> tuple[int, int]:
    deleted = 0
    failed = 0
    offers = store.list_offers(None if include_marked_deleted else "active")
    for offer in offers:
        if is_structured_offer_text(offer.text):
            continue
        if (
            include_marked_deleted
            and offer.status.startswith("deleted:")
            and offer.status.endswith(":verified")
        ):
            continue
        reason = (
            "legacy-format:verified"
            if include_marked_deleted and offer.status != "active"
            else "legacy-format"
        )
        if await delete_offer(
            senders,
            destination,
            store,
            offer.fingerprint,
            reason,
            include_inactive=include_marked_deleted,
        ):
            deleted += 1
        else:
            failed += 1
    return deleted, failed

async def verify_marked_deleted_offers(
    *,
    senders: Iterable[TelegramClient],
    destination: object | None,
    store: DedupeStore,
) -> tuple[int, int]:
    verified = 0
    failed = 0
    for offer in store.list_offers(None):
        if not offer.status.startswith("deleted:") or offer.status.endswith(":verified"):
            continue
        reason = f"{offer.status.removeprefix('deleted:')}:verified"
        if await delete_offer(
            senders,
            destination,
            store,
            offer.fingerprint,
            reason,
            include_inactive=True,
        ):
            verified += 1
        else:
            failed += 1
    return verified, failed

async def reformat_active_offers(
    *,
    senders: Iterable[TelegramClient],
    destination: object | None,
    store: DedupeStore,
    max_chars: int,
) -> tuple[int, int]:
    if destination is None:
        return 0, len(store.list_active_offers())

    updated = 0
    failed = 0
    for offer in store.list_active_offers():
        if not is_structured_offer_text(offer.text):
            continue
        styled_body = ensure_offer_body_style(offer.text)
        if styled_body == offer.text:
            continue
        rendered = build_offer_publish_text_from_body(
            body=styled_body,
            category=offer.category,
            sources=store.offer_sources(offer.fingerprint),
            max_chars=max_chars,
        )
        if await edit_offer_message(senders, destination, offer, rendered):
            if styled_body != offer.text:
                store.update_offer_text(offer.fingerprint, styled_body, offer.source_count)
            updated += 1
        else:
            failed += 1
    return updated, failed

async def recategorize_active_offers(
    *,
    senders: Iterable[TelegramClient],
    destination: object | None,
    store: DedupeStore,
    max_chars: int,
    limit: int,
    only_altro: bool = True,
) -> tuple[int, int, int, int]:
    if destination is None:
        return 0, 0, 0, 1

    scanned = 0
    updated = 0
    deleted = 0
    failed = 0
    for offer in store.list_active_offers():
        if scanned >= limit:
            break
        if only_altro and offer.category != "altro":
            continue
        urls = await resolve_offer_urls(offer_record_urls(offer))
        if not urls:
            continue
        scanned += 1
        try:
            site_text = await category_context_for_offer_urls(urls)
            new_category = classify_category(offer.text, site_text)
        except Exception as exc:
            LOGGER.warning("Could not recategorize offer %s: %s", offer.fingerprint, exc)
            failed += 1
            continue

        if new_category == offer.category:
            continue

        store.update_offer_category(offer.fingerprint, new_category)
        refreshed = store.get_offer(offer.fingerprint) or offer
        if not passes_filters(store, new_category, offer.price):
            if await delete_offer(
                senders,
                destination,
                store,
                offer.fingerprint,
                "recategorized-filtered",
            ):
                deleted += 1
            else:
                failed += 1
            continue

        if is_menu_only_enabled(store):
            updated += 1
            continue

        rendered = build_offer_publish_text_from_body(
            body=refreshed.text,
            category=new_category,
            sources=store.offer_sources(offer.fingerprint),
            max_chars=max_chars,
        )
        if await edit_offer_message(senders, destination, refreshed, rendered):
            updated += 1
        else:
            failed += 1
    return scanned, updated, deleted, failed

async def purge_unmerged_destination_messages(
    *,
    reader: TelegramClient,
    senders: Iterable[TelegramClient],
    destination: object | None,
    store: DedupeStore,
    limit: int,
) -> tuple[int, int, int]:
    if destination is None:
        return 0, 0, 1
    if is_private_user_destination(destination):
        return await purge_unmerged_private_bot_dialog_messages(
            senders=senders,
            destination=destination,
            store=store,
            limit=limit,
        )

    groups: list[list[PublishedOfferMessage]] = []
    try:
        async for message in reader.iter_messages(destination, limit=limit):
            published = await parse_published_offer_message(message)
            if published is None:
                continue
            for group in groups:
                if any(published_offers_match(published, existing) for existing in group):
                    group.append(published)
                    break
            else:
                groups.append([published])
    except Exception as exc:
        LOGGER.warning("Could not scan destination history for unmerged duplicates: %s", exc)
        return 0, 0, 1

    active_by_message_id = {
        offer.primary_message_id: offer
        for offer in store.list_active_offers()
    }
    deleted = 0
    groups_with_duplicates = 0
    failed = 0
    for group in groups:
        if len(group) < 2:
            continue
        groups_with_duplicates += 1
        group_ids = {item.message_id for item in group}
        tracked_ids = [
            item.message_id
            for item in group
            if item.message_id in active_by_message_id
        ]
        keep_id = tracked_ids[0] if tracked_ids else min(group_ids)
        delete_ids = sorted(group_ids - {keep_id})
        if not delete_ids:
            continue
        if await delete_messages_with_fallback(senders, destination, delete_ids):
            deleted += len(delete_ids)
            for message_id in delete_ids:
                tracked = active_by_message_id.get(message_id)
                if tracked is not None:
                    store.mark_offer_status(tracked.fingerprint, "deleted:unmerged-destination")
        else:
            failed += len(delete_ids)

    return deleted, groups_with_duplicates, failed
