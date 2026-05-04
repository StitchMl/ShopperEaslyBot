from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
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
from .normalization import build_fingerprint, canonicalize_url, normalize_text, resolve_redirect_url
from .offer_analysis import (
    analyze_offer,
    classify_category,
    known_filter_categories,
    parse_price_limit,
    source_score,
)
from .site_context import combined_site_context


LOGGER = logging.getLogger("shopper_merge_bot")
CAPTION_LIMIT = 1024
BOT_API_BASE = "https://api.telegram.org/bot"
PRIVATE_DELETE_SCAN_LIMIT = 10000
PRIVATE_DELETE_CACHE_SECONDS = 180.0
PRIVATE_DIALOG_CACHES: dict[tuple[int, str], "PrivateDialogCache"] = {}


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
    product: str | None
    original_price: Decimal | None
    current_price: Decimal | None
    category: str | None = None


@dataclass
class PrivateDialogCache:
    loaded_at: float
    user_client: TelegramClient
    bot_entity: object
    message_id_offset: int
    messages: list[PublishedOfferMessage]


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
    text_result = await bot_api_request(
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

    caption_result = await bot_api_request(
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
        result = await bot_api_request(
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


def category_matches_filter(category: str, selected: str) -> bool:
    if category == selected:
        return True
    return "/" not in selected and category.startswith(f"{selected}/")


def passes_filters(store: DedupeStore, category: str, price: object | None) -> bool:
    categories = tuple(item for item in store.get_filter_categories() if item != "altro")
    if categories and not any(category_matches_filter(category, item) for item in categories):
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
        if await bot_api_delete_messages(token, destination, ids):
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
    if not await delete_messages_with_fallback(senders, destination, ids, offer=offer):
        LOGGER.error("Could not delete offer %s after trying all clients", fingerprint)
        return False

    store.mark_offer_status(fingerprint, f"deleted:{reason}")
    return True


def fingerprints_from_urls(urls: Iterable[str]) -> set[str]:
    return {fingerprint_for_offer_url(url) for url in urls if url}


def strip_product_suffixes(product: str) -> str:
    cleaned = re.sub(
        r"\b(?:scende|sceso|sale|salito|torna)\b.*$",
        "",
        product,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:minimo storico|record minimo|offerta lampo|super prezzo)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", cleaned).strip(" -,:")


def product_similarity(left: str, right: str) -> float:
    left_tokens = set(normalize_text(strip_product_suffixes(left)).split())
    right_tokens = set(normalize_text(strip_product_suffixes(right)).split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))


def extract_offer_label(text: str, label: str) -> str | None:
    pattern = rf"(?:[^\w\s]|\ufe0f|\s)*{re.escape(label)}\s*(.+)"
    for line in text.splitlines():
        match = re.match(pattern, line.strip(), flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def offer_record_product(offer: OfferRecord) -> str | None:
    return extract_offer_label(offer.text, "Prodotto:")


def offer_record_original_price(offer: OfferRecord) -> Decimal | None:
    value = extract_offer_label(offer.text, "Prezzo originale:")
    return parse_price_limit(value.lower()) if value else None


def is_similar_offer(
    offer: OfferRecord,
    *,
    product: str,
    original_price: object,
    current_price: object,
) -> bool:
    if offer.price != current_price:
        return False
    existing_product = offer_record_product(offer)
    if not existing_product:
        return False
    if offer_record_original_price(offer) != original_price:
        return False
    return product_similarity(existing_product, product) >= 0.78


def find_similar_active_offer(
    store: DedupeStore,
    *,
    product: str,
    original_price: object,
    current_price: object,
    excluded_fingerprint: str,
) -> OfferRecord | None:
    for offer in store.list_active_offers():
        if offer.fingerprint == excluded_fingerprint:
            continue
        if is_similar_offer(
            offer,
            product=product,
            original_price=original_price,
            current_price=current_price,
        ):
            return offer
    return None


def offer_record_urls(offer: OfferRecord) -> tuple[str, ...]:
    return tuple(re.findall(r"https?://[^\s<>()\"']+", offer.text or ""))


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
        product=product,
        original_price=parse_price_limit(original.lower()) if original else None,
        current_price=parse_price_limit(current.lower()) if current else None,
        category=category.lower() if category else None,
    )


def offer_record_as_published(offer: OfferRecord) -> PublishedOfferMessage:
    return PublishedOfferMessage(
        message_id=offer.primary_message_id,
        fingerprint=offer.fingerprint,
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
    mapped_fingerprints: set[str] = set()
    for candidate_source_id in source_ids:
        mapped_fingerprints.update(
            store.fingerprints_for_source_message(candidate_source_id, message_id)
        )
    site_category_context = await category_context_for_offer_urls(offer_urls)
    facts = analyze_offer(raw_text, offer_urls, site_text=site_category_context)
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
        mark_seen_message(store, source_ids, message_id)
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
        mark_seen_message(store, source_ids, message_id)
        LOGGER.info("Incomplete offer skipped from %s/%s", source_id, message_id)
        return

    already_seen = any(
        store.has_message(candidate_source_id, message_id)
        for candidate_source_id in source_ids
    )
    if already_seen and not allow_seen_update:
        return

    searchable_text = raw_text or ""
    if settings.allow_patterns and not matches_any(searchable_text, settings.allow_patterns):
        mark_seen_message(store, source_ids, message_id)
        return
    if settings.skip_patterns and matches_any(searchable_text, settings.skip_patterns):
        mark_seen_message(store, source_ids, message_id)
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
        mark_seen_message(store, source_ids, message_id)
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
            if added:
                sources = store.offer_sources(target_fingerprint)
                merged = build_offer_publish_text_from_body(
                    body=existing.text,
                    category=existing.category,
                    sources=sources,
                    max_chars=settings.max_text_chars,
                )
                store.update_offer_text(target_fingerprint, existing.text, len(sources))
                if not settings.dry_run:
                    await edit_offer_message(cleanup_senders, destination, existing, merged)
                LOGGER.info("Merged duplicate offer %s from %s/%s", target_fingerprint, source_id, message_id)
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
            mark_seen_message(store, source_ids, message_id)
            LOGGER.info("Delivered message from %s/%s", source_id, message_id)
        except Exception:
            raise


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
        "/purge_deleted - ritenta le cancellazioni gia' segnate nel database",
        "/purge_unmerged [numero] - elimina duplicati vecchi non registrati nel DB",
        "/purge_history [numero] [keep=N] - ripulisce la chat privata da offerte vecchie/duplicate/fuori filtro",
        "/recategorize [numero|all] - ricalcola categorie usando il sito dell'offerta",
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
        available = set(known_filter_categories())
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

    @bot.on(events.NewMessage(pattern=r"^/purge_deleted(?:@\w+)?(?:\s|$)"))
    async def on_purge_deleted(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return
        if state.destination is None:
            await event.respond("Destinazione non configurata.", parse_mode=None)
            return

        verified, failed = await verify_marked_deleted_offers(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
        )
        await event.respond(
            f"Cancellazioni gia' segnate ritentate: {verified}"
            + (f"\nAncora non cancellabili: {failed}" if failed else ""),
            parse_mode=None,
        )

    @bot.on(events.NewMessage(pattern=r"^/(purge_unmerged|purge_duplicates)(?:@\w+)?(?:\s|$)"))
    async def on_purge_unmerged(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return
        if state.destination is None:
            await event.respond("Destinazione non configurata.", parse_mode=None)
            return

        arg = command_arg(event.raw_text or "")
        limit = 500
        if arg.strip().isdigit():
            limit = min(max(int(arg.strip()), 50), 5000)
        await event.respond(f"Scansiono gli ultimi {limit} messaggi della destinazione...", parse_mode=None)
        deleted, groups, failed = await purge_unmerged_destination_messages(
            reader=bot,
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
            limit=limit,
        )
        await event.respond(
            "\n".join(
                [
                    "Purge duplicati completato.",
                    f"Gruppi duplicati trovati: {groups}",
                    f"Messaggi eliminati: {deleted}",
                    f"Eliminazioni fallite: {failed}",
                ]
            ),
            parse_mode=None,
        )

    @bot.on(events.NewMessage(pattern=r"^/purge_history(?:@\w+)?(?:\s|$)"))
    async def on_purge_history(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return
        if state.destination is None:
            await event.respond("Destinazione non configurata.", parse_mode=None)
            return
        if not is_private_user_destination(state.destination):
            await event.respond(
                "Questo comando e' pensato per la chat privata col bot. "
                "Per gruppi/canali usa /purge_unmerged e /reconcile.",
                parse_mode=None,
            )
            return

        args = command_arg(event.raw_text or "").lower().split()
        limit = 5000
        keep_latest: int | None = None
        for item in args:
            if item.isdigit():
                limit = min(max(int(item), 50), 20000)
            elif item.startswith("keep="):
                keep_value = item.split("=", 1)[1]
                if keep_value.isdigit():
                    keep_latest = min(max(int(keep_value), 50), 5000)

        await event.respond(
            f"Scansiono fino a {limit} offerte nella chat privata"
            + (f" e tengo le ultime {keep_latest} valide..." if keep_latest is not None else "..."),
            parse_mode=None,
        )
        scanned, filtered, duplicates, trimmed, kept, failed = await purge_private_history_messages(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
            limit=limit,
            keep_latest=keep_latest,
        )
        await event.respond(
            "\n".join(
                [
                    "Pulizia cronologia completata.",
                    f"Offerte scansionate: {scanned}",
                    f"Fuori filtro eliminate: {filtered}",
                    f"Duplicate eliminate: {duplicates}",
                    f"Vecchie oltre limite eliminate: {trimmed}",
                    f"Offerte valide tenute: {kept}",
                    f"Eliminazioni fallite: {failed}",
                ]
            ),
            parse_mode=None,
        )

    @bot.on(events.NewMessage(pattern=r"^/recategorize(?:@\w+)?(?:\s|$)"))
    async def on_recategorize(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return
        if state.destination is None:
            await event.respond("Destinazione non configurata.", parse_mode=None)
            return

        args = command_arg(event.raw_text or "").lower().split()
        only_altro = "all" not in args
        limit = 200
        for item in args:
            if item.isdigit():
                limit = min(max(int(item), 10), 5000)
        await event.respond(
            "Ricalcolo categorie usando sito/prodotto per "
            + (f"{limit} offerte non categorizzate..." if only_altro else f"{limit} offerte attive..."),
            parse_mode=None,
        )
        scanned, updated, deleted, failed = await recategorize_active_offers(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
            max_chars=settings.max_text_chars,
            limit=limit,
            only_altro=only_altro,
        )
        await event.respond(
            "\n".join(
                [
                    "Ricategorizzazione completata.",
                    f"Offerte analizzate: {scanned}",
                    f"Messaggi aggiornati: {updated}",
                    f"Offerte rimosse dai filtri: {deleted}",
                    f"Operazioni fallite: {failed}",
                ]
            ),
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
        renamed, merged, merge_failed = await merge_duplicate_active_offers(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
            max_chars=settings.max_text_chars,
        )
        unmerged_deleted, unmerged_groups, unmerged_failed = await purge_unmerged_destination_messages(
            reader=bot,
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
            limit=500,
        )
        legacy_deleted, legacy_failed = await purge_legacy_offers(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
            include_marked_deleted=True,
        )
        verified_deleted, verified_failed = await verify_marked_deleted_offers(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
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
                    f"Fingerprint canonici aggiornati: {renamed}",
                    f"Duplicati uniti: {merged}",
                    f"Duplicati non registrati eliminati: {unmerged_deleted}",
                    f"Legacy eliminati: {legacy_deleted}",
                    f"Cancellazioni gia' segnate verificate: {verified_deleted}",
                    f"Messaggi riformattati: {reformatted}",
                    f"Operazioni fallite: {filtered_failed + merge_failed + unmerged_failed + legacy_failed + verified_failed + reformat_failed}",
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
            renamed, merged, merge_failed = await merge_duplicate_active_offers(
                senders=cleanup_senders,
                destination=state.destination,
                store=store,
                max_chars=settings.max_text_chars,
            )
            unmerged_deleted, unmerged_groups, unmerged_failed = await purge_unmerged_destination_messages(
                reader=sender,
                senders=cleanup_senders,
                destination=state.destination,
                store=store,
                limit=500,
            )
            legacy_deleted, legacy_failed = await purge_legacy_offers(
                senders=cleanup_senders,
                destination=state.destination,
                store=store,
                include_marked_deleted=True,
            )
            verified_deleted, verified_failed = await verify_marked_deleted_offers(
                senders=cleanup_senders,
                destination=state.destination,
                store=store,
            )
            reformatted, reformat_failed = await reformat_active_offers(
                senders=cleanup_senders,
                destination=state.destination,
                store=store,
                max_chars=settings.max_text_chars,
            )
            LOGGER.info(
                "Startup reconcile: filtered_deleted=%s filtered_failed=%s "
                "renamed=%s merged=%s merge_failed=%s unmerged_deleted=%s "
                "unmerged_groups=%s unmerged_failed=%s legacy_deleted=%s "
                "legacy_failed=%s verified_deleted=%s verified_failed=%s "
                "reformatted=%s reformat_failed=%s",
                filtered_deleted,
                filtered_failed,
                renamed,
                merged,
                merge_failed,
                unmerged_deleted,
                unmerged_groups,
                unmerged_failed,
                legacy_deleted,
                legacy_failed,
                verified_deleted,
                verified_failed,
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
            state=state,
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
                source_ids = message_source_ids(new_message_event.message)
                if not settings.monitor_all_chats and not any(
                    source_id in state.source_ids for source_id in source_ids
                ):
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
                if not settings.monitor_all_chats and not any(
                    source_id in state.source_ids for source_id in source_ids
                ):
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
                if not source_ids:
                    return
                if not settings.monitor_all_chats and not any(
                    source_id in state.source_ids for source_id in source_ids
                ):
                    return
                for deleted_id in deleted_event.deleted_ids:
                    deleted_fingerprints: set[str] = set()
                    for source_id in source_ids:
                        deleted_fingerprints.update(
                            store.fingerprints_for_source_message(
                                source_id,
                                int(deleted_id),
                            )
                        )
                    for fingerprint in deleted_fingerprints:
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
