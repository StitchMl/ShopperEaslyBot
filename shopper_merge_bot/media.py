from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
from pathlib import Path
from typing import Iterable

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError
from telethon.tl.types import Message, PeerChannel, PeerChat, PeerUser

from .constants import (
    CAPTION_LIMIT,
    PRODUCT_GIF_FETCH_TIMEOUT_SECONDS,
    PRODUCT_GIF_FRAME_DURATION_MS,
    PRODUCT_GIF_MAX_FRAMES,
    PRODUCT_GIF_MAX_SIDE,
    PRODUCT_GIF_UPLOAD_TIMEOUT_SECONDS,
)
from .dedupe import OfferRecord, OfferSource
from .formatter import trim_text


LOGGER = logging.getLogger("shopper_merge_bot")


def message_ids_from_result(result: object) -> list[int]:
    if isinstance(result, list):
        return [int(item.id) for item in result if hasattr(item, "id")]
    if hasattr(result, "id"):
        return [int(result.id)]
    return []


async def send_with_retry(
    sender: TelegramClient,
    destination: object,
    message: Message,
    text: str,
    copy_media: bool,
    limiter: object,
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
                    message_ids = message_ids_from_result(result)
                    if len(text) > CAPTION_LIMIT:
                        extra = await sender.send_message(
                            destination,
                            text,
                            link_preview=True,
                            parse_mode=None,
                        )
                        message_ids.extend(message_ids_from_result(extra))
                    return message_ids
        except Exception:
            LOGGER.exception("Could not copy source media; sending text-only fallback")

    result = await sender.send_message(
        destination,
        text,
        link_preview=True,
        parse_mode=None,
    )
    return message_ids_from_result(result)


def image_hash(image: object) -> str:
    try:
        from PIL import Image
    except ModuleNotFoundError:
        return ""

    if not isinstance(image, Image.Image):
        return ""
    thumbnail = image.copy()
    thumbnail.thumbnail((160, 160), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (160, 160), "white")
    x = (canvas.width - thumbnail.width) // 2
    y = (canvas.height - thumbnail.height) // 2
    canvas.paste(thumbnail, (x, y))
    return hashlib.sha256(canvas.tobytes()).hexdigest()


def normalized_gif_frame(image: object) -> object:
    from PIL import Image

    if image.mode in {"RGBA", "LA", "P"}:
        converted = image.convert("RGBA")
        background = Image.new("RGBA", converted.size, "white")
        alpha = converted.getchannel("A") if "A" in converted.getbands() else None
        background.paste(converted, mask=alpha)
        converted = background.convert("RGB")
    else:
        converted = image.convert("RGB")
    converted.thumbnail((PRODUCT_GIF_MAX_SIDE, PRODUCT_GIF_MAX_SIDE), Image.Resampling.LANCZOS)
    return converted.copy()


def create_product_gif(image_paths: Iterable[Path], output_path: Path) -> bool:
    try:
        from PIL import Image, ImageSequence, UnidentifiedImageError
    except ModuleNotFoundError:
        LOGGER.warning("Pillow is not installed; product GIF creation is disabled")
        return False

    frames = []
    seen_hashes: set[str] = set()
    for image_path in image_paths:
        if len(frames) >= PRODUCT_GIF_MAX_FRAMES:
            break
        try:
            with Image.open(image_path) as image:
                for raw_frame in ImageSequence.Iterator(image):
                    frame = normalized_gif_frame(raw_frame)
                    fingerprint = image_hash(frame)
                    if fingerprint and fingerprint not in seen_hashes:
                        seen_hashes.add(fingerprint)
                        frames.append(frame)
                    if len(frames) >= PRODUCT_GIF_MAX_FRAMES:
                        break
        except (OSError, UnidentifiedImageError) as exc:
            LOGGER.debug("Skipping non-image media %s while creating product GIF: %s", image_path, exc)
            continue

    if len(frames) < 2:
        return False

    width = max(frame.width for frame in frames)
    height = max(frame.height for frame in frames)
    rendered = []
    for frame in frames:
        canvas = Image.new("RGB", (width, height), "white")
        x = (width - frame.width) // 2
        y = (height - frame.height) // 2
        canvas.paste(frame, (x, y))
        rendered.append(canvas)

    rendered[0].save(
        output_path,
        save_all=True,
        append_images=rendered[1:],
        duration=PRODUCT_GIF_FRAME_DURATION_MS,
        loop=0,
        optimize=True,
    )
    return output_path.exists()


async def download_message_media(message: Message, directory: Path) -> Path | None:
    if not getattr(message, "media", None):
        return None
    try:
        downloaded = await asyncio.wait_for(
            message.download_media(file=str(directory)),
            timeout=PRODUCT_GIF_FETCH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        LOGGER.debug("Timed out downloading media for product GIF")
        return None
    if not downloaded:
        return None
    path = Path(downloaded)
    return path if path.exists() else None


async def get_destination_message_with_media(
    senders: Iterable[TelegramClient],
    destination: object,
    message_id: int,
) -> Message | None:
    for sender in senders:
        try:
            message = await asyncio.wait_for(
                sender.get_messages(destination, ids=message_id),
                timeout=PRODUCT_GIF_FETCH_TIMEOUT_SECONDS,
            )
            if isinstance(message, list):
                message = message[0] if message else None
            if message is not None and getattr(message, "media", None):
                return message
        except Exception as exc:
            LOGGER.debug(
                "Could not fetch destination media message %s with %s: %s",
                message_id,
                sender.session.__class__.__name__,
                exc,
            )
    return None


def source_entity_candidates(source_chat_id: str) -> tuple[object, ...]:
    candidates: list[object] = []

    def add(value: object) -> None:
        if value not in candidates:
            candidates.append(value)

    cleaned = source_chat_id.strip()
    try:
        numeric = int(cleaned)
    except ValueError:
        if cleaned:
            add(cleaned)
        return tuple(candidates)

    try:
        resolved_id, peer_type = utils.resolve_id(numeric)
        if peer_type is PeerChannel:
            add(PeerChannel(resolved_id))
        elif peer_type is PeerChat:
            add(PeerChat(resolved_id))
        elif peer_type is PeerUser:
            add(PeerUser(resolved_id))
    except Exception:
        pass

    add(numeric)
    if cleaned:
        add(cleaned)
    if cleaned.startswith("-100") and len(cleaned) > 4:
        try:
            add(int(cleaned[4:]))
        except ValueError:
            pass
    return tuple(candidates)


async def get_source_message(
    reader: TelegramClient,
    source: OfferSource,
) -> Message | None:
    for entity in source_entity_candidates(source.source_chat_id):
        try:
            message = await asyncio.wait_for(
                reader.get_messages(entity, ids=source.source_message_id),
                timeout=PRODUCT_GIF_FETCH_TIMEOUT_SECONDS,
            )
            if isinstance(message, list):
                message = message[0] if message else None
            if message is not None:
                return message
        except Exception as exc:
            LOGGER.debug(
                "Could not fetch source media %s/%s via %r: %s",
                source.source_chat_id,
                source.source_message_id,
                entity,
                exc,
            )
    return None


async def edit_offer_media_as_gif(
    senders: Iterable[TelegramClient],
    destination: object,
    offer: OfferRecord,
    source_message: Message,
    text: str,
) -> bool:
    if not getattr(source_message, "media", None):
        return False

    senders_tuple = tuple(senders)
    with tempfile.TemporaryDirectory(prefix="shopperbot-gif-") as temp_dir:
        temp_path = Path(temp_dir)
        source_dir = temp_path / "source"
        target_dir = temp_path / "target"
        source_dir.mkdir()
        target_dir.mkdir()

        source_media = await download_message_media(source_message, source_dir)
        if source_media is None:
            return False

        target_message = await get_destination_message_with_media(
            senders_tuple,
            destination,
            offer.primary_message_id,
        )
        if target_message is None:
            return False

        target_media = await download_message_media(target_message, target_dir)
        if target_media is None:
            return False

        gif_path = temp_path / "product-images.gif"
        if not create_product_gif((target_media, source_media), gif_path):
            return False

        caption = trim_text(text, CAPTION_LIMIT)
        for sender in senders_tuple:
            try:
                await sender.edit_message(
                    destination,
                    offer.primary_message_id,
                    caption,
                    file=gif_path,
                    parse_mode=None,
                )
                return True
            except Exception as exc:
                LOGGER.warning(
                    "Could not update offer %s media GIF with %s: %s",
                    offer.fingerprint,
                    sender.session.__class__.__name__,
                    exc,
                )
    return False
