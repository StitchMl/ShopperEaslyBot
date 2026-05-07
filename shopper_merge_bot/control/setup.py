from __future__ import annotations

from .context import ControlContext
from .deps import *  # noqa: F403


def register(ctx: ControlContext) -> None:
    _register_on_help(ctx)
    _register_on_whoami(ctx)
    _register_on_claim(ctx)
    _register_on_destination_here(ctx)
    _register_on_destination(ctx)
    _register_on_folder(ctx)
    _register_on_find(ctx)


def _register_on_help(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

    @bot.on(events.NewMessage(pattern=r"^/(start|help)(?:@\w+)?(?:\s|$)"))
    async def on_help(event: events.NewMessage.Event) -> None:
        await event.respond(control_help(event.sender_id), parse_mode=None)

def _register_on_whoami(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

    @bot.on(events.NewMessage(pattern=r"^/whoami(?:@\w+)?(?:\s|$)"))
    async def on_whoami(event: events.NewMessage.Event) -> None:
        await event.respond(
            f"user_id={event.sender_id}\nchat_id={event.chat_id}",
            parse_mode=None,
        )

def _register_on_claim(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

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

def _register_on_destination_here(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

    @bot.on(events.NewMessage(pattern=r"^/destination_here(?:@\w+)?(?:\s|$)"))
    async def on_destination_here(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return

        store.set_config("destination_chat", str(event.chat_id))
        await refresh_destination(bot, store, state)
        await event.respond("Destinazione impostata su questa chat.", parse_mode=None)

def _register_on_destination(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

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

def _register_on_folder(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

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

def _register_on_find(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

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
