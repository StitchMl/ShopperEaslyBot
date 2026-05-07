from __future__ import annotations

from telethon import TelegramClient

from .config import Settings
from .control import filters, maintenance, menu, purge, setup, sources
from .control.context import ControlContext
from .dedupe import DedupeStore
from .runtime import RuntimeState, unique_clients


async def register_control_bot(
    *,
    bot: TelegramClient,
    source_client: TelegramClient,
    settings: Settings,
    store: DedupeStore,
    state: RuntimeState,
) -> None:
    ctx = ControlContext(
        bot=bot,
        source_client=source_client,
        settings=settings,
        store=store,
        state=state,
        cleanup_senders=unique_clients(bot, source_client),
    )
    menu.register(ctx)
    setup.register(ctx)
    filters.register(ctx)
    sources.register(ctx)
    purge.register(ctx)
    maintenance.register(ctx)
