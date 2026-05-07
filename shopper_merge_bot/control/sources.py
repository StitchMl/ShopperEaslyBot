from __future__ import annotations

from .context import ControlContext
from .deps import *  # noqa: F403


def register(ctx: ControlContext) -> None:
    _register_on_scan_sources(ctx)
    _register_on_add(ctx)
    _register_on_sources(ctx)
    _register_on_clear_sources(ctx)


def _register_on_scan_sources(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

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

def _register_on_add(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

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

def _register_on_sources(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

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

def _register_on_clear_sources(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

    @bot.on(events.NewMessage(pattern=r"^/clear_sources(?:@\w+)?(?:\s|$)"))
    async def on_clear_sources(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return

        store.clear_sources()
        state.source_ids = set()
        await event.respond("Sorgenti svuotate. Usa /folder <link> per ricaricarle.", parse_mode=None)
