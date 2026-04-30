from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

from telethon import TelegramClient, events, utils
from telethon.errors import AccessTokenInvalidError, ApiIdInvalidError, FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import Channel, Chat, Message, User

from .chat_folder import import_chat_folder
from .config import ConfigError, Settings, parse_chat_ref
from .dedupe import DedupeStore, OfferRecord
from .formatter import trim_text
from .normalization import build_fingerprint, canonicalize_url
from .offer_analysis import (
    CATEGORY_KEYWORDS,
    analyze_offer,
    parse_price_limit,
    source_score,
)


LOGGER = logging.getLogger("shopper_merge_bot")
CAPTION_LIMIT = 1024


@dataclass
class RuntimeState:
    source_ids: set[str] = field(default_factory=set)
    destination_ref: str | None = None
    destination: object | None = None
    ignored_chat_ids: set[str] = field(default_factory=set)
    control_bot_peer_id: str | None = None


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


def _message_ids_from_result(result: object) -> list[int]:
    if isinstance(result, list):
        return [int(item.id) for item in result if hasattr(item, "id")]
    if hasattr(result, "id"):
        return [int(result.id)]
    return []


def destination_peer_id(destination: object) -> str:
    return str(utils.get_peer_id(destination))


def format_price(value: object) -> str:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return f"{value} EUR"
    return f"{amount:.2f}".replace(".", ",") + " EUR"


def savings_line(original_price: object, current_price: object) -> str | None:
    try:
        original = Decimal(str(original_price))
        current = Decimal(str(current_price))
    except (InvalidOperation, ValueError):
        return None
    if original <= 0 or current >= original:
        return None

    saving = (original - current).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    discount = ((saving / original) * Decimal("100")).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )
    return f"✅ Risparmio stimato: {format_price(saving)} ({discount}%)"


def build_offer_publish_text(
    *,
    product: str,
    original_price: object,
    current_price: object,
    offer_url: str,
    category: str,
    sources: list[tuple[str, str]],
    max_chars: int,
) -> str:
    text = build_offer_publish_text_from_body(
        body=stable_offer_body(
            product=product,
            original_price=original_price,
            current_price=current_price,
            offer_url=offer_url,
        ),
        category=category,
        sources=sources,
        max_chars=max_chars,
    )
    return text


def build_offer_publish_text_from_body(
    *,
    body: str,
    category: str,
    sources: list[tuple[str, str]],
    max_chars: int,
) -> str:
    text = ensure_offer_body_style(body)
    if category:
        text += f"\n\n📂 Categoria: {category}"
    if len(sources) > 1:
        text += f"\n🔁 Confermata da {len(sources)} fonti"
    return trim_text(text, max_chars)


def stable_offer_body(*, product: str, original_price: object, current_price: object, offer_url: str) -> str:
    lines = [
        "🔥 Offerta pronta da controllare",
        "",
        f"📦 Prodotto: {product}",
        "",
        f"💸 Prezzo attuale: {format_price(current_price)}",
        f"🏷️ Prezzo originale: {format_price(original_price)}",
    ]
    saving = savings_line(original_price, current_price)
    if saving:
        lines.append(saving)
    lines.extend(["", f"🔗 Link offerta: {offer_url}"])
    return "\n".join(lines)


def ensure_offer_body_style(text: str) -> str:
    replacements = (
        ("Offerta pronta da controllare", "🔥 Offerta pronta da controllare"),
        ("Prodotto:", "📦 Prodotto:"),
        ("Prezzo attuale:", "💸 Prezzo attuale:"),
        ("Prezzo originale:", "🏷️ Prezzo originale:"),
        ("Risparmio stimato:", "✅ Risparmio stimato:"),
        ("Link offerta:", "🔗 Link offerta:"),
    )
    styled_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        for plain, styled in replacements:
            if stripped == plain or stripped.startswith(f"{plain} "):
                leading = line[: len(line) - len(line.lstrip())]
                line = leading + styled + stripped[len(plain) :]
                break
        styled_lines.append(line)
    return "\n".join(styled_lines)


def is_structured_offer_text(text: str) -> bool:
    required_labels = (
        "Prodotto:",
        "Prezzo originale:",
        "Prezzo attuale:",
        "Link offerta:",
    )
    return all(label in text for label in required_labels)


def fingerprint_for_offer_url(url: str) -> str:
    return build_fingerprint(canonicalize_url(url), fallback=url)


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


def passes_filters(store: DedupeStore, category: str, price: object | None) -> bool:
    categories = store.get_filter_categories()
    if categories and category not in categories:
        return False

    min_price = store.get_filter_price("filter_min_price")
    max_price = store.get_filter_price("filter_max_price")
    if min_price is None and max_price is None:
        return True
    if price is None:
        return False
    if min_price is not None and price < min_price:
        return False
    if max_price is not None and price > max_price:
        return False
    return True


async def send_with_retry(
    sender: TelegramClient,
    destination: object,
    message: Message,
    text: str,
    copy_media: bool,
    limiter: RateLimiter,
) -> list[int]:
    await limiter.wait()
    try:
        return await send_once(sender, destination, message, text, copy_media)
    except FloodWaitError as exc:
        LOGGER.warning("Telegram flood wait: sleeping %s seconds", exc.seconds)
        await asyncio.sleep(exc.seconds + 1)
        await limiter.wait()
        return await send_once(sender, destination, message, text, copy_media)


async def send_once(
    sender: TelegramClient,
    destination: object,
    message: Message,
    text: str,
    copy_media: bool,
) -> list[int]:
    if copy_media and message.media:
        try:
            with tempfile.TemporaryDirectory(prefix="shopperbot-") as temp_dir:
                downloaded = await message.download_media(file=temp_dir)
                if downloaded:
                    caption = trim_text(text, CAPTION_LIMIT)
                    result = await sender.send_file(
                        destination,
                        file=Path(downloaded),
                        caption=caption,
                        parse_mode=None,
                    )
                    message_ids = _message_ids_from_result(result)
                    if len(text) > CAPTION_LIMIT:
                        extra = await sender.send_message(
                            destination,
                            text,
                            link_preview=True,
                            parse_mode=None,
                        )
                        message_ids.extend(_message_ids_from_result(extra))
                    return message_ids
        except Exception:
            LOGGER.exception("Could not copy source media; sending text-only fallback")

    result = await sender.send_message(
        destination,
        text,
        link_preview=True,
        parse_mode=None,
    )
    return _message_ids_from_result(result)


async def edit_offer_message(
    senders: Iterable[TelegramClient],
    destination: object,
    offer: OfferRecord,
    text: str,
) -> bool:
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
) -> bool:
    for sender in senders:
        try:
            await sender.delete_messages(destination, ids)
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
    if not await delete_messages_with_fallback(senders, destination, ids):
        LOGGER.error("Could not delete offer %s after trying all clients", fingerprint)
        return False

    store.mark_offer_status(fingerprint, f"deleted:{reason}")
    return True


def fingerprints_from_urls(urls: Iterable[str]) -> set[str]:
    return {fingerprint_for_offer_url(url) for url in urls if url}


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


async def handle_message(
    *,
    message: Message,
    settings: Settings,
    store: DedupeStore,
    sender: TelegramClient,
    cleanup_senders: tuple[TelegramClient, ...],
    destination: object | None,
    limiter: RateLimiter,
    ignored_chat_ids: set[str],
    allow_seen_update: bool = False,
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

    raw_text = message.raw_text or ""
    if not raw_text and not message.media:
        store.mark_message(source_id, message_id)
        return

    offer_urls = message_offer_urls(message)
    mapped_fingerprints = set(store.fingerprints_for_source_message(source_id, message_id))
    facts = analyze_offer(raw_text, offer_urls)
    if facts.invalid:
        expired_fingerprints = mapped_fingerprints | fingerprints_from_urls(offer_urls)
        for fingerprint in expired_fingerprints:
            await delete_offer(
                cleanup_senders,
                destination,
                store,
                fingerprint,
                "invalid-source-edit",
            )
        store.mark_message(source_id, message_id)
        return

    if not facts.complete:
        if allow_seen_update:
            for fingerprint in mapped_fingerprints:
                await delete_offer(
                    cleanup_senders,
                    destination,
                    store,
                    fingerprint,
                    "incomplete-source-edit",
                )
        store.mark_message(source_id, message_id)
        LOGGER.info("Incomplete offer skipped from %s/%s", source_id, message_id)
        return

    if store.has_message(source_id, message_id) and not allow_seen_update:
        return

    searchable_text = raw_text or ""
    if settings.allow_patterns and not matches_any(searchable_text, settings.allow_patterns):
        store.mark_message(source_id, message_id)
        return
    if settings.skip_patterns and matches_any(searchable_text, settings.skip_patterns):
        store.mark_message(source_id, message_id)
        return

    source_link = await source_permalink(message) if settings.include_source_link else None
    title = await chat_title(message)
    if not passes_filters(store, facts.category, facts.price):
        if allow_seen_update:
            for fingerprint in mapped_fingerprints:
                await delete_offer(
                    cleanup_senders,
                    destination,
                    store,
                    fingerprint,
                    "source-edited-filtered",
                )
        LOGGER.info(
            "Filtered message from %s/%s category=%s price=%s",
            source_id,
            message_id,
            facts.category,
            facts.price,
        )
        store.mark_message(source_id, message_id)
        return

    fingerprint = fingerprint_for_offer_url(str(facts.offer_url))
    normalized_body = stable_offer_body(
        product=str(facts.product),
        original_price=facts.original_price,
        current_price=facts.current_price,
        offer_url=str(facts.offer_url),
    )
    stale_fingerprints = mapped_fingerprints - {fingerprint}
    for stale_fingerprint in stale_fingerprints:
        await delete_offer(
            cleanup_senders,
            destination,
            store,
            stale_fingerprint,
            "source-edited-new-offer",
        )

    existing = store.get_offer(fingerprint)
    if existing and existing.status == "active":
        added = store.add_offer_source(
            fingerprint=fingerprint,
            source_chat_id=source_id,
            source_message_id=message_id,
            source_title=title,
            source_link=source_link or "",
        )
        store.mark_message(source_id, message_id)
        if added:
            sources = store.offer_sources(fingerprint)
            merged = build_offer_publish_text_from_body(
                body=existing.text,
                category=existing.category,
                sources=sources,
                max_chars=settings.max_text_chars,
            )
            store.update_offer_text(fingerprint, existing.text, len(sources))
            if not settings.dry_run:
                await edit_offer_message(cleanup_senders, destination, existing, merged)
            LOGGER.info("Merged duplicate offer %s from %s/%s", fingerprint, source_id, message_id)
        return

    outbound = build_offer_publish_text(
        product=str(facts.product),
        original_price=facts.original_price,
        current_price=facts.current_price,
        offer_url=str(facts.offer_url),
        category=facts.category,
        sources=[(title, source_link or "")],
        max_chars=settings.max_text_chars,
    )

    try:
        message_ids: list[int] = []
        if settings.dry_run:
            LOGGER.info("DRY_RUN message from %s/%s:\n%s", source_id, message_id, outbound)
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
        store.mark_message(source_id, message_id)
        LOGGER.info("Delivered message from %s/%s", source_id, message_id)
    except Exception:
        raise


async def run_backfill(
    *,
    client: TelegramClient,
    sources: list[object],
    settings: Settings,
    store: DedupeStore,
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
                sender=sender,
                cleanup_senders=cleanup_senders,
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
        "/filters - mostra filtri categoria/prezzo",
        "/category <nomi|clear|list> - filtra per categoria",
        "/price <min|max|clear> [valore] - filtra per prezzo",
        "/scan_sources [offerte|notizie] - aggiunge bot, canali e gruppi compatibili",
        "/scan_bots [offerte|notizie] - alias che scansiona tutte le sorgenti",
        "/purge_legacy - elimina offerte pubblicate col vecchio formato",
        "/purge_legacy hard - ritenta anche vecchie eliminazioni fallite",
        "/reconcile - sincronizza filtri, merge e formato dei messaggi gia' pubblicati",
        "/diagnose_destination - prova invio, modifica e cancellazione nella destinazione",
        "/sources - mostra le sorgenti attive",
        "/clear_sources - svuota le sorgenti",
        "/status - stato del servizio",
        "/whoami - mostra user id e chat id",
    ]
    return "\n".join(lines)


def filters_text(store: DedupeStore) -> str:
    categories = store.get_filter_categories()
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
            ", ".join(sorted(CATEGORY_KEYWORDS.keys()) + ["altro"]),
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
    cleanup_senders = unique_clients(bot, source_client)

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
            if source.peer_id == state.control_bot_peer_id:
                continue
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
            if is_control_bot_entity(state, entity):
                continue
            title = str(dialog.name or entity_title(entity))
            username = str(getattr(entity, "username", "") or "")
            searchable = f"{title} {username} {dialog.id}".lower()
            if query not in searchable:
                continue
            kind = entity_kind(entity)
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

    @bot.on(events.NewMessage(pattern=r"^/filters(?:@\w+)?(?:\s|$)"))
    async def on_filters(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return
        await event.respond(filters_text(store), parse_mode=None)

    @bot.on(events.NewMessage(pattern=r"^/category(?:@\w+)?(?:\s|$)"))
    async def on_category(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return

        arg = command_arg(event.raw_text or "").strip().lower()
        available = set(CATEGORY_KEYWORDS.keys()) | {"altro"}
        if not arg or arg == "list":
            await event.respond(
                "Categorie disponibili:\n" + ", ".join(sorted(available)),
                parse_mode=None,
            )
            return
        if arg == "clear":
            store.set_filter_categories(())
            await event.respond("Filtro categorie disattivato.", parse_mode=None)
            return

        categories = tuple(
            item.strip().lower()
            for item in re.split(r"[, ]+", arg)
            if item.strip()
        )
        unknown = [item for item in categories if item not in available]
        if unknown:
            await event.respond(
                "Categorie non riconosciute: "
                + ", ".join(unknown)
                + "\nUsa /category list.",
                parse_mode=None,
            )
            return

        store.set_filter_categories(categories)
        deleted, failed = await purge_filtered_offers(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
        )
        await event.respond(
            "Filtro categorie impostato: "
            + ", ".join(categories)
            + f"\nOfferte gia' pubblicate rimosse: {deleted}"
            + (f"\nRimozioni fallite: {failed}" if failed else ""),
            parse_mode=None,
        )

    @bot.on(events.NewMessage(pattern=r"^/price(?:@\w+)?(?:\s|$)"))
    async def on_price(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return

        args = command_arg(event.raw_text or "").split()
        if not args:
            await event.respond(
                "Uso:\n/price max 50\n/price min 10\n/price clear",
                parse_mode=None,
            )
            return
        mode = args[0].lower()
        if mode == "clear":
            store.set_filter_price("filter_min_price", None)
            store.set_filter_price("filter_max_price", None)
            await event.respond("Filtro prezzo disattivato.", parse_mode=None)
            return
        if mode not in {"min", "max"} or len(args) < 2:
            await event.respond(
                "Uso:\n/price max 50\n/price min 10\n/price clear",
                parse_mode=None,
            )
            return
        value = parse_price_limit(args[1])
        if value is None:
            await event.respond("Prezzo non valido.", parse_mode=None)
            return
        key = "filter_min_price" if mode == "min" else "filter_max_price"
        store.set_filter_price(key, value)
        deleted, failed = await purge_filtered_offers(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
        )
        await event.respond(
            filters_text(store)
            + f"\n\nOfferte gia' pubblicate rimosse: {deleted}"
            + (f"\nRimozioni fallite: {failed}" if failed else ""),
            parse_mode=None,
        )

    @bot.on(events.NewMessage(pattern=r"^/(scan_sources|scan_bots)(?:@\w+)?(?:\s|$)"))
    async def on_scan_sources(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return

        args = command_arg(event.raw_text or "").lower().split()
        mode = args[0] if args and args[0] in {"offerte", "notizie"} else "offerte"
        threshold = 2
        if len(args) > 1 and args[1].isdigit():
            threshold = int(args[1])

        await event.respond(
            f"Scansiono bot, canali e gruppi visibili per: {mode}...",
            parse_mode=None,
        )
        added = []
        skipped = []
        async for dialog in source_client.iter_dialogs():
            entity = dialog.entity
            if is_control_bot_entity(state, entity):
                continue
            peer_id = entity_peer_id(entity)
            title = str(dialog.name or entity_title(entity))
            username = str(getattr(entity, "username", "") or "")
            kind = entity_kind(entity)
            score = source_score(title, username, mode, kind)
            if score < threshold:
                skipped.append(title)
                continue
            save_source_entity(store, entity)
            added.append((title, username, score, peer_id, kind))

        state.source_ids = store.source_ids()
        lines = [f"Scan completato: {len(added)} sorgenti aggiunte per {mode}."]
        for title, username, score, dialog_id, kind in added[:40]:
            handle = f" @{username}" if username else ""
            lines.append(f"- [{kind}] {title}{handle} ({dialog_id}) score={score}")
        if len(added) > 40:
            lines.append(f"... e altre {len(added) - 40}")
        if not added:
            lines.append(
                "Nessuna sorgente compatibile trovata. Usa /find o abbassa soglia: "
                "/scan_sources offerte 1"
            )
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

        if is_control_bot_entity(state, entity):
            store.remove_source(entity_peer_id(entity))
            state.source_ids.discard(entity_peer_id(entity))
            await event.respond(
                "Non posso aggiungere il bot aggregatore come sorgente: verrebbe letto "
                "di nuovo dai suoi stessi messaggi.",
                parse_mode=None,
            )
            return

        save_source_entity(store, entity)
        state.source_ids = store.source_ids()
        await event.respond(
            f"Sorgente aggiunta: {entity_title(entity)} ({entity_peer_id(entity)})",
            parse_mode=None,
        )

    @bot.on(events.NewMessage(pattern=r"^/purge_legacy(?:@\w+)?(?:\s|$)"))
    async def on_purge_legacy(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return
        if state.destination is None:
            await event.respond("Destinazione non configurata.", parse_mode=None)
            return

        include_marked_deleted = command_arg(event.raw_text or "").lower() == "hard"
        deleted, failed = await purge_legacy_offers(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
            include_marked_deleted=include_marked_deleted,
        )
        await event.respond(
            f"Pulizia completata. Messaggi legacy eliminati: {deleted}"
            + (f"\nEliminazioni fallite: {failed}" if failed else ""),
            parse_mode=None,
        )

    @bot.on(events.NewMessage(pattern=r"^/(reconcile|maintenance)(?:@\w+)?(?:\s|$)"))
    async def on_reconcile(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return
        if state.destination is None:
            await event.respond("Destinazione non configurata.", parse_mode=None)
            return

        await event.respond("Riconcilio messaggi pubblicati, filtri e vecchi formati...", parse_mode=None)
        filtered_deleted, filtered_failed = await purge_filtered_offers(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
        )
        legacy_deleted, legacy_failed = await purge_legacy_offers(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
            include_marked_deleted=True,
        )
        reformatted, reformat_failed = await reformat_active_offers(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
            max_chars=settings.max_text_chars,
        )
        await event.respond(
            "\n".join(
                [
                    "Riconciliazione completata.",
                    f"Fuori filtro eliminati: {filtered_deleted}",
                    f"Legacy eliminati: {legacy_deleted}",
                    f"Messaggi riformattati: {reformatted}",
                    f"Operazioni fallite: {filtered_failed + legacy_failed + reformat_failed}",
                ]
            ),
            parse_mode=None,
        )

    @bot.on(events.NewMessage(pattern=r"^/diagnose_destination(?:@\w+)?(?:\s|$)"))
    async def on_diagnose_destination(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return
        if state.destination is None:
            await event.respond("Destinazione non configurata.", parse_mode=None)
            return

        sent_ids: list[int] = []
        send_ok = False
        edit_ok = False
        delete_ok = False
        error = ""
        try:
            result = await bot.send_message(
                state.destination,
                "Test Shopper Easly: invio in corso...",
                parse_mode=None,
            )
            sent_ids = _message_ids_from_result(result)
            send_ok = bool(sent_ids)
            if send_ok:
                fake_offer = OfferRecord(
                    fingerprint="diagnose",
                    destination_chat_id=destination_peer_id(state.destination),
                    primary_message_id=sent_ids[0],
                    extra_message_ids=tuple(sent_ids[1:]),
                    text="diagnose",
                    category="diagnose",
                    price=None,
                    source_count=1,
                    status="active",
                )
                edit_ok = await edit_offer_message(
                    cleanup_senders,
                    state.destination,
                    fake_offer,
                    "Test Shopper Easly: modifica riuscita.",
                )
                delete_ok = await delete_messages_with_fallback(
                    cleanup_senders,
                    state.destination,
                    sent_ids,
                )
        except Exception as exc:
            error = str(exc)

        await event.respond(
            "\n".join(
                [
                    "Diagnosi destinazione",
                    f"Invio: {'ok' if send_ok else 'fallito'}",
                    f"Modifica: {'ok' if edit_ok else 'fallita'}",
                    f"Cancellazione: {'ok' if delete_ok else 'fallita'}",
                    (
                        "Se modifica/cancellazione falliscono, rendi il bot admin della "
                        "destinazione con permessi di modificare e cancellare messaggi."
                    ),
                    f"Errore: {error}" if error else "",
                ]
            ).strip(),
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
                    "",
                    filters_text(store),
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
            bot_session_path = settings.database_path.parent / "control_bot"
            sender_client = TelegramClient(
                str(bot_session_path),
                settings.telegram_api_id,
                settings.telegram_api_hash,
            )
            while True:
                try:
                    await sender_client.start(bot_token=settings.telegram_bot_token)
                    break
                except FloodWaitError as exc:
                    LOGGER.warning(
                        "Telegram asked to wait %s seconds before bot authorization; sleeping",
                        exc.seconds,
                    )
                    await asyncio.sleep(exc.seconds + 5)
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

        cleanup_senders = unique_clients(sender, source_client)
        if state.destination is not None:
            filtered_deleted, filtered_failed = await purge_filtered_offers(
                senders=cleanup_senders,
                destination=state.destination,
                store=store,
            )
            legacy_deleted, legacy_failed = await purge_legacy_offers(
                senders=cleanup_senders,
                destination=state.destination,
                store=store,
                include_marked_deleted=True,
            )
            reformatted, reformat_failed = await reformat_active_offers(
                senders=cleanup_senders,
                destination=state.destination,
                store=store,
                max_chars=settings.max_text_chars,
            )
            LOGGER.info(
                "Startup reconcile: filtered_deleted=%s filtered_failed=%s "
                "legacy_deleted=%s legacy_failed=%s reformatted=%s reformat_failed=%s",
                filtered_deleted,
                filtered_failed,
                legacy_deleted,
                legacy_failed,
                reformatted,
                reformat_failed,
            )
        limiter = RateLimiter(settings.min_post_interval_seconds)
        sources = await resolve_saved_sources(source_client, store)
        await run_backfill(
            client=source_client,
            sources=sources,
            settings=settings,
            store=store,
            sender=sender,
            cleanup_senders=cleanup_senders,
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
                source_id = str(
                    edited_event.message.chat_id
                    or utils.get_peer_id(edited_event.message.peer_id)
                )
                if not settings.monitor_all_chats and source_id not in state.source_ids:
                    return

                await handle_message(
                    message=edited_event.message,
                    settings=settings,
                    store=store,
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
                if deleted_event.chat_id is None:
                    return
                source_id = str(deleted_event.chat_id)
                if not settings.monitor_all_chats and source_id not in state.source_ids:
                    return
                for deleted_id in deleted_event.deleted_ids:
                    for fingerprint in store.fingerprints_for_source_message(
                        source_id,
                        int(deleted_id),
                    ):
                        await delete_offer(
                            cleanup_senders,
                            state.destination,
                            store,
                            fingerprint,
                            "source-deleted",
                        )
            except Exception:
                LOGGER.exception("Could not process deleted message")

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
