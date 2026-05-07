from __future__ import annotations

from .context import ControlContext
from .deps import *  # noqa: F403


def register(ctx: ControlContext) -> None:
    _register_on_menu_callback(ctx)


def _register_on_menu_callback(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

    @bot.on(events.CallbackQuery(pattern=rb"^menu:"))
    async def on_menu_callback(event: events.CallbackQuery.Event) -> None:
        sender_id = int(event.sender_id or 0)
        if not store.is_admin(sender_id, settings.admin_user_ids):
            await event.answer("Non autorizzato.", alert=True)
            return

        parsed = parse_menu_callback_data(event.data or b"")
        if parsed is None:
            await event.answer("Menu non valido.", alert=True)
            return

        action, menu_key, page = parsed
        if action == "close":
            if state.destination is not None:
                deleted = False
                if menu_key:
                    deleted = await delete_offer_menu(
                        senders=cleanup_senders,
                        destination=state.destination,
                        store=store,
                        menu_key=open_menu_storage_key(menu_key),
                    )
                if not deleted:
                    await delete_messages_with_fallback(
                        cleanup_senders,
                        state.destination,
                        [int(event.message_id)],
                    )
            else:
                await event.delete()
            await event.answer("Chiuso.")
            return

        offers = grouped_active_offers(store).get(menu_key, [])
        if state.destination is None:
            await event.answer("Destinazione non configurata.", alert=True)
            return

        await event.answer("Apro categoria...")
        source_reader = await first_user_client(cleanup_senders) or source_client
        _sent, _failed = await expand_offer_menu(
            senders=cleanup_senders,
            source_reader=source_reader,
            destination=state.destination,
            store=store,
            menu_key=menu_key,
            offers=offers,
            max_chars=settings.max_text_chars,
            pause_seconds=settings.min_post_interval_seconds,
        )
        await upsert_menu_index(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
        )
        if _failed:
            await event.respond(
                f"Categoria aperta, ma {_failed} prodotti non sono stati inviati.",
                parse_mode=None,
            )
