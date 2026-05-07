from __future__ import annotations

from dataclasses import dataclass

from telethon import TelegramClient

from shopper_merge_bot.config import Settings
from shopper_merge_bot.dedupe import DedupeStore
from shopper_merge_bot.runtime import RuntimeState


@dataclass(frozen=True)
class ControlContext:
    bot: TelegramClient
    source_client: TelegramClient
    settings: Settings
    store: DedupeStore
    state: RuntimeState
    cleanup_senders: tuple[TelegramClient, ...]
