from __future__ import annotations

from .context import ControlContext
from .deps import *  # noqa: F403


def register(ctx: ControlContext) -> None:
    _register_on_recategorize(ctx)
    _register_on_refresh_gifs(ctx)
    _register_on_reconcile(ctx)
    _register_on_diagnose_destination(ctx)
    _register_on_status(ctx)


def _register_on_recategorize(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

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
        menu_updated = menu_deleted = menu_failed = 0
        if is_menu_only_enabled(store):
            menu_updated, menu_deleted, menu_failed = await sync_offer_menus(
                senders=cleanup_senders,
                destination=state.destination,
                store=store,
                max_chars=settings.max_text_chars,
            )
        await event.respond(
            "\n".join(
                [
                    "Ricategorizzazione completata.",
                    f"Offerte analizzate: {scanned}",
                    f"Messaggi aggiornati: {updated}",
                    f"Offerte rimosse dai filtri: {deleted}",
                    f"Menu aggiornati: {menu_updated}",
                    f"Menu rimossi: {menu_deleted}",
                    f"Operazioni fallite: {failed + menu_failed}",
                ]
            ),
            parse_mode=None,
        )

def _register_on_refresh_gifs(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

    @bot.on(events.NewMessage(pattern=r"^/refresh_gifs(?:@\w+)?(?:\s|$)"))
    async def on_refresh_gifs(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return
        if state.destination is None:
            await event.respond("Destinazione non configurata.", parse_mode=None)
            return

        arg = command_arg(event.raw_text or "").lower()
        limit = 200
        if arg in {"all", "tutte", "tutti"}:
            limit = 0
        elif arg.isdigit():
            limit = min(max(int(arg), 1), 5000)

        await event.respond(
            "Rigenero le GIF prodotto per "
            + ("tutte le offerte unite..." if limit <= 0 else f"fino a {limit} offerte unite..."),
            parse_mode=None,
        )
        source_reader = await first_user_client(cleanup_senders) or source_client
        scanned, updated, skipped, failed = await refresh_active_offer_gifs(
            senders=cleanup_senders,
            source_reader=source_reader,
            destination=state.destination,
            store=store,
            max_chars=settings.max_text_chars,
            limit=limit,
        )
        await event.respond(
            "\n".join(
                [
                    "Rigenerazione GIF completata.",
                    f"Offerte unite controllate: {scanned}",
                    f"GIF aggiornate: {updated}",
                    f"Senza abbastanza immagini diverse: {skipped}",
                    f"Operazioni fallite: {failed}",
                ]
            ),
            parse_mode=None,
        )

def _register_on_reconcile(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

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
        expired_scanned, expired_deleted, expired_active, expired_unknown, expired_failed = await purge_inactive_link_offers(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
            limit=settings.expired_offer_check_limit,
        )
        renamed, merged, merge_failed = await merge_duplicate_active_offers(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
            max_chars=settings.max_text_chars,
        )
        menu_updated = menu_deleted = menu_failed = 0
        if is_menu_only_enabled(store):
            unmerged_deleted = unmerged_groups = unmerged_failed = 0
            legacy_deleted = legacy_failed = 0
            verified_deleted = verified_failed = 0
            reformatted = reformat_failed = 0
            menu_updated, menu_deleted, menu_failed = await sync_offer_menus(
                senders=cleanup_senders,
                destination=state.destination,
                store=store,
                max_chars=settings.max_text_chars,
            )
            if is_private_user_destination(state.destination):
                _legacy_scanned, legacy_deleted, legacy_failed = await purge_private_structured_offer_messages(
                    senders=cleanup_senders,
                    destination=state.destination,
                    limit=PRIVATE_DELETE_SCAN_LIMIT,
                )
        else:
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
                    f"Link offerte controllati: {expired_scanned}",
                    f"Offerte terminate eliminate: {expired_deleted}",
                    f"Link non determinabili: {expired_unknown}",
                    f"Fingerprint canonici aggiornati: {renamed}",
                    f"Duplicati uniti: {merged}",
                    f"Duplicati non registrati eliminati: {unmerged_deleted}",
                    f"Legacy eliminati: {legacy_deleted}",
                    f"Cancellazioni gia' segnate verificate: {verified_deleted}",
                    f"Messaggi riformattati: {reformatted}",
                    f"Menu aggiornati: {menu_updated}",
                    f"Menu rimossi: {menu_deleted}",
                    f"Operazioni fallite: {filtered_failed + expired_failed + merge_failed + unmerged_failed + legacy_failed + verified_failed + reformat_failed + menu_failed}",
                ]
            ),
            parse_mode=None,
        )

def _register_on_diagnose_destination(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

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

def _register_on_status(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

    @bot.on(events.NewMessage(pattern=r"^/status(?:@\w+)?(?:\s|$)"))
    async def on_status(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return

        await event.respond(
            "\n".join(
                [
                    "Stato Shopper Easly",
                    f"Versione: {__version__}",
                    f"Sorgenti: {len(state.source_ids)}",
                    f"Destinazione: {state.destination_ref or 'non impostata'}",
                    f"Monitor all chats: {settings.monitor_all_chats}",
                    f"Dry run: {settings.dry_run}",
                    f"Modalita pubblicazione: {publish_mode(store)}",
                    f"Menu attivi: {len(store.list_menu_messages())}",
                    f"Pulizia offerte terminate: /purge_expired disponibile",
                    f"Controllo periodico offerte terminate: {settings.expired_offer_check_interval_seconds}s",
                    "",
                    filters_text(store),
                ]
            ),
            parse_mode=None,
        )
