from __future__ import annotations

from .context import ControlContext
from .deps import *  # noqa: F403


def register(ctx: ControlContext) -> None:
    _register_on_filters(ctx)
    _register_on_category(ctx)
    _register_on_price(ctx)
    _register_on_publish_mode(ctx)
    _register_on_menu_sync(ctx)
    _register_on_purge_product_posts(ctx)


def _register_on_filters(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

    @bot.on(events.NewMessage(pattern=r"^/filters(?:@\w+)?(?:\s|$)"))
    async def on_filters(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return
        await event.respond(filters_text(store), parse_mode=None)

def _register_on_category(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

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
        menu_updated = menu_deleted = menu_failed = 0
        if is_menu_only_enabled(store):
            menu_updated, menu_deleted, menu_failed = await sync_offer_menus(
                senders=cleanup_senders,
                destination=state.destination,
                store=store,
                max_chars=settings.max_text_chars,
            )
        await event.respond(
            "Filtro categorie impostato: "
            + ", ".join(categories)
            + f"\nOfferte gia' pubblicate rimosse: {deleted}"
            + (f"\nMenu aggiornati: {menu_updated}" if is_menu_only_enabled(store) else "")
            + (f"\nMenu rimossi: {menu_deleted}" if menu_deleted else "")
            + (f"\nRimozioni fallite: {failed + menu_failed}" if failed or menu_failed else ""),
            parse_mode=None,
        )

def _register_on_price(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

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
        menu_updated = menu_deleted = menu_failed = 0
        if is_menu_only_enabled(store):
            menu_updated, menu_deleted, menu_failed = await sync_offer_menus(
                senders=cleanup_senders,
                destination=state.destination,
                store=store,
                max_chars=settings.max_text_chars,
            )
        await event.respond(
            filters_text(store)
            + f"\n\nOfferte gia' pubblicate rimosse: {deleted}"
            + (f"\nMenu aggiornati: {menu_updated}" if is_menu_only_enabled(store) else "")
            + (f"\nMenu rimossi: {menu_deleted}" if menu_deleted else "")
            + (f"\nRimozioni fallite: {failed + menu_failed}" if failed or menu_failed else ""),
            parse_mode=None,
        )

def _register_on_publish_mode(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

    @bot.on(events.NewMessage(pattern=r"^/publish_mode(?:@\w+)?(?:\s|$)"))
    async def on_publish_mode(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return
        arg = command_arg(event.raw_text or "").strip().lower()
        if not arg:
            await event.respond(f"Modalita pubblicazione: {publish_mode(store)}", parse_mode=None)
            return
        if arg not in {"posts", "post", "menu", "menu-only", "menu_only"}:
            await event.respond("Uso: /publish_mode posts oppure /publish_mode menu", parse_mode=None)
            return
        mode = set_publish_mode(store, arg)
        if mode == PUBLISH_MODE_MENU_ONLY:
            menu_updated, found_posts, deleted_posts, failed = await migrate_active_posts_to_menu_only(
                senders=cleanup_senders,
                destination=state.destination,
                store=store,
                max_chars=settings.max_text_chars,
            )
            await event.respond(
                "\n".join(
                    [
                        "Modalita menu-only attivata.",
                        f"Menu aggiornati: {menu_updated}",
                        f"Vecchi post prodotto trovati: {found_posts}",
                        f"Vecchi post prodotto rimossi: {deleted_posts}",
                        f"Operazioni fallite: {failed}",
                    ]
                ),
                parse_mode=None,
            )
            return
        await event.respond(
            "Modalita post singoli attivata. Le nuove offerte torneranno a essere pubblicate come messaggi separati.",
            parse_mode=None,
        )

def _register_on_menu_sync(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

    @bot.on(events.NewMessage(pattern=r"^/menu_sync(?:@\w+)?(?:\s|$)"))
    async def on_menu_sync(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return
        if state.destination is None:
            await event.respond("Destinazione non configurata.", parse_mode=None)
            return
        await event.respond("Aggiorno tutti i menu offerte...", parse_mode=None)
        updated, deleted, failed = await sync_offer_menus(
            senders=cleanup_senders,
            destination=state.destination,
            store=store,
            max_chars=settings.max_text_chars,
        )
        legacy_scanned = legacy_deleted = legacy_failed = 0
        if is_menu_only_enabled(store) and is_private_user_destination(state.destination):
            legacy_scanned, legacy_deleted, legacy_failed = await purge_private_structured_offer_messages(
                senders=cleanup_senders,
                destination=state.destination,
                limit=PRIVATE_DELETE_SCAN_LIMIT,
            )
        await event.respond(
            "\n".join(
                [
                    "Menu aggiornati.",
                    f"Menu scritti/aggiornati: {updated}",
                    f"Menu rimossi: {deleted}",
                    f"Vecchi messaggi prodotto trovati: {legacy_scanned}",
                    f"Vecchi messaggi prodotto rimossi: {legacy_deleted}",
                    f"Operazioni fallite: {failed + legacy_failed}",
                ]
            ),
            parse_mode=None,
        )

def _register_on_purge_product_posts(ctx: ControlContext) -> None:
    bot = ctx.bot
    source_client = ctx.source_client
    settings = ctx.settings
    store = ctx.store
    state = ctx.state
    cleanup_senders = ctx.cleanup_senders

    @bot.on(events.NewMessage(pattern=r"^/purge_product_posts(?:@\w+)?(?:\s|$)"))
    async def on_purge_product_posts(event: events.NewMessage.Event) -> None:
        if not await is_control_admin(event, settings, store):
            return
        if state.destination is None:
            await event.respond("Destinazione non configurata.", parse_mode=None)
            return
        if not is_private_user_destination(state.destination):
            await event.respond(
                "Questo comando e' disponibile per la chat privata col bot.",
                parse_mode=None,
            )
            return

        arg = command_arg(event.raw_text or "").strip().lower()
        limit = PRIVATE_DELETE_SCAN_LIMIT
        if arg and arg != "all":
            if not arg.isdigit():
                await event.respond("Uso: /purge_product_posts [numero|all]", parse_mode=None)
                return
            limit = min(max(int(arg), 50), PRIVATE_DELETE_SCAN_LIMIT)

        await event.respond("Elimino i vecchi messaggi prodotto e lascio solo il menu...", parse_mode=None)
        scanned, deleted, failed = await purge_private_structured_offer_messages(
            senders=cleanup_senders,
            destination=state.destination,
            limit=limit,
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
                    "Pulizia messaggi prodotto completata.",
                    f"Messaggi prodotto trovati: {scanned}",
                    f"Messaggi prodotto rimossi: {deleted}",
                    f"Menu aggiornati: {menu_updated}",
                    f"Menu rimossi: {menu_deleted}",
                    f"Operazioni fallite: {failed + menu_failed}",
                ]
            ),
            parse_mode=None,
        )
