from __future__ import annotations

from .context import ControlContext
from .deps import *  # noqa: F403


def register(ctx: ControlContext) -> None:
    _register_on_purge_legacy(ctx)
    _register_on_purge_deleted(ctx)
    _register_on_purge_expired(ctx)
    _register_on_purge_unmerged(ctx)
    _register_on_purge_history(ctx)


def _register_on_purge_legacy(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

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

def _register_on_purge_deleted(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

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

def _register_on_purge_expired(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

    @bot.on(events.NewMessage(pattern=r"^/purge_expired(?:@\w+)?(?:\s|$)"))
    async def on_purge_expired(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return
        if state.destination is None:
            await event.respond("Destinazione non configurata.", parse_mode=None)
            return

        arg = command_arg(event.raw_text or "").lower()
        limit = settings.expired_offer_check_limit
        if arg in {"all", "tutte", "tutti"}:
            limit = 0
        elif arg.isdigit():
            limit = min(max(int(arg), 1), 5000)

        scope = "tutte le offerte attive e la cronologia della destinazione" if limit <= 0 else f"fino a {limit} offerte attive e messaggi della destinazione"
        await event.respond(f"Controllo i link di {scope}...", parse_mode=None)
        tracked_scanned, tracked_deleted, tracked_active, tracked_unknown, tracked_failed = await purge_inactive_link_offers(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
            limit=limit,
        )
        history_scanned, history_deleted, history_active, history_unknown, history_failed = await purge_inactive_published_messages(
            reader=bot,
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
            limit=limit,
        )
        await event.respond(
            "\n".join(
                [
                    "Controllo link completato.",
                    f"Offerte DB controllate: {tracked_scanned}",
                    f"Messaggi destinazione controllati: {history_scanned}",
                    f"Messaggi eliminati per offerta terminata: {tracked_deleted + history_deleted}",
                    f"Link ancora attivi: {tracked_active + history_active}",
                    f"Stato non determinabile: {tracked_unknown + history_unknown}",
                    f"Operazioni fallite: {tracked_failed + history_failed}",
                ]
            ),
            parse_mode=None,
        )

def _register_on_purge_unmerged(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

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

def _register_on_purge_history(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

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
