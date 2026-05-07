from __future__ import annotations

import time
from datetime import datetime
from typing import Iterable

from telethon import Button

from .dedupe import DedupeStore, OfferRecord
from .filters import passes_filters
from .formatter import trim_text
from .normalization import normalize_text
from .offers import format_price, offer_record_product, offer_record_urls


PUBLISH_MODE_POSTS = "posts"
PUBLISH_MODE_MENU_ONLY = "menu-only"
MENU_NEW_WINDOW_SECONDS = 24 * 60 * 60
MENU_MAX_OFFERS = 25
MENU_DETAIL_PAGE_SIZE = 10
MENU_INDEX_KEY = "menu:index"
MENU_OPEN_PREFIX = "menu:open:"
MENU_SEEN_PREFIX = "menu_seen:"

MENU_TYPE_RULES: tuple[tuple[tuple[str, ...], str, str, tuple[str, ...]], ...] = (
    (("elettronica",), "cuffie", "Cuffie", ("cuffie", "auricolari", "headphones", "earbuds", "quietcomfort")),
    (("elettronica",), "casse", "Casse e speaker", ("speaker", "cassa", "casse", "altoparlante", "soundbar", "xboom", "jbl")),
    (("elettronica",), "accessori-tech", "Accessori tech", ("caricatore", "powerbank", "cavo usb", "usb-c", "magsafe", "mouse", "tastiera", "adattatore usb")),
    (("elettronica",), "rete-wifi", "Rete e Wi-Fi", ("router", "wifi", "wi-fi", "saponetta wifi", "hotspot", "tp-link", "tplink", "antenna wifi", "archer")),
    (("elettronica",), "componenti-pc", "Componenti PC", ("dissipatore", "cpu", "scheda madre", "ram", "corsair", "nautilus")),
    (("elettronica",), "stampanti", "Stampanti", ("stampante", "stampanti", "cartuccia", "cartucce", "zoemini", "laserjet", "deskjet")),
    (("elettronica",), "smart-home", "Smart home e sicurezza", ("tapo", "telecamera wifi", "telecamera", "camera wifi", "videosorveglianza")),
    (("elettronica",), "computer", "Computer", ("notebook", "laptop", "computer", "mini pc", "desktop", "macbook", "pavilion", "elitebook")),
    (("elettronica",), "monitor", "Monitor", ("monitor", "fullhd", "full hd", "qhd", "uhd")),
    (("elettronica",), "smartphone", "Smartphone", ("smartphone", "telefono", "iphone", "samsung galaxy", "xiaomi", "redmi", "oneplus", "oppo", "realme")),
    (("elettronica",), "storage", "Storage", ("ssd", "hard disk", "hdd", "microsd", "micro sd", "scheda sd", "memory card", "sandisk")),
    (("elettronica",), "tv-video", "TV, foto e video", ("tv", "televisore", "proiettore", "projector", "videocamera", "fotocamera", "drone", "dji", "kodak")),
    (("elettronica",), "gaming", "Gaming", ("console", "playstation", "xbox", "nintendo", "gaming")),
    (("giochi",), "lego", "LEGO", ("lego",)),
    (("giochi",), "videogiochi", "Videogiochi", ("videogioco", "videogiochi", "nintendo", "super mario", "playstation", "xbox", "switch")),
    (("giochi",), "giochi-bambini", "Giochi bambini", ("giocattolo", "bambola", "bambole", "peluche", "playmobil", "hot wheels", "barbie", "gormi")),
    (("software",), "licenze", "Licenze e codici", ("licenza", "codice digitale", "gift card", "windows", "office", "antivirus")),
    (("software",), "abbonamenti", "Abbonamenti", ("abbonamento", "vpn", "cloud storage", "playstation plus", "xbox game pass")),
    (("casa",), "cucina", "Cucina", ("cucina", "friggitrice", "forno", "microonde", "pentola", "padella", "barbecue", "weber")),
    (("casa",), "bagno", "Bagno", ("bagno", "porta carta igienica", "portarotolo", "doccia", "rubinetto")),
    (("casa",), "arredo", "Arredo", ("scarpiera", "cornice", "mobile", "mensola", "tappeto", "cuscino", "zanzariera", "tenda")),
    (("casa",), "illuminazione", "Illuminazione", ("lampada", "lampadine", "lampadina", "led", "illuminazione")),
    (("casa",), "pulizia", "Pulizia casa", ("aspirapolvere", "pulizia", "detersivo", "ammorbidente")),
    (("fai-da-te",), "utensili", "Utensili", ("utensile", "trapano", "avvitatore", "paranco", "puleggia", "ribimex", "chiave regolabile", "seghetto", "smerigliatrice", "idropulitrice", "wolfcraft")),
    (("fai-da-te",), "giardino", "Giardino", ("tagliaerba", "motosega", "catena di ricambio", "oregon", "greenworks")),
    (("fai-da-te",), "elettrico", "Materiale elettrico", ("avvolgicavo", "spina", "presa multipla", "presa universale", "vimar", "electraline", "schuko")),
    (("fai-da-te",), "ferramenta", "Ferramenta", ("vite", "viti", "tassello", "tasselli", "bullone", "bulloni", "forgefix", "fischer", "dado")),
    (("fai-da-te",), "sicurezza", "Sicurezza lavoro", ("casco di sicurezza", "portwest", "lucchetto a chiave", "master lock")),
    (("bellezza",), "makeup", "Make-up", ("fondotinta", "makeup", "make up", "smalto", "rossetto", "mascara")),
    (("bellezza",), "cura-persona", "Cura persona", ("crema", "shampoo", "rasoio", "spazzolino", "deodorante", "profumo")),
    (("animali",), "gatti", "Gatti", ("gatto", "gatti", "lettiera", "inaba")),
    (("animali",), "cani", "Cani", ("cane", "cani")),
)

MENU_CATEGORY_TITLES = {
    "accessori": "Accessori",
    "alimentari": "Alimentari",
    "animali": "Animali",
    "auto": "Auto",
    "bellezza": "Bellezza",
    "casa": "Casa",
    "elettronica": "Elettronica",
    "fai-da-te": "Fai-da-te",
    "giochi": "Giochi",
    "infanzia": "Infanzia",
    "libri": "Libri",
    "moda": "Moda",
    "musica": "Musica",
    "software": "Software",
    "sport": "Sport",
    "ufficio": "Ufficio",
    "viaggi": "Viaggi",
}


def publish_mode(store: DedupeStore) -> str:
    configured = (store.get_config("publish_mode") or PUBLISH_MODE_POSTS).strip().lower()
    return PUBLISH_MODE_MENU_ONLY if configured in {"menu", "menu-only", "menu_only"} else PUBLISH_MODE_POSTS


def is_menu_only_enabled(store: DedupeStore) -> bool:
    return publish_mode(store) == PUBLISH_MODE_MENU_ONLY


def set_publish_mode(store: DedupeStore, mode: str) -> str:
    normalized = PUBLISH_MODE_MENU_ONLY if mode in {"menu", "menu-only", "menu_only"} else PUBLISH_MODE_POSTS
    store.set_config("publish_mode", normalized)
    return normalized


def keyword_in_normalized_text(keyword: str, text: str) -> bool:
    keyword = normalize_text(keyword)
    if not keyword:
        return False
    if " " in keyword:
        return keyword in text
    return f" {keyword} " in f" {text} "


def menu_category(category: str) -> str:
    return category.split("/", 1)[0].strip().lower() or "altro"


def menu_category_title(category: str) -> str:
    return MENU_CATEGORY_TITLES.get(category, category.replace("-", " ").title())


def menu_slug_title(category: str, slug: str) -> str:
    if not slug or slug == "all":
        return menu_category_title(category)
    for categories, rule_slug, title, _keywords in MENU_TYPE_RULES:
        if category in categories and slug == rule_slug:
            return title
    return slug.replace("-", " ").title()


def offer_menu_type(offer: OfferRecord) -> tuple[str, str]:
    category = menu_category(offer.category)
    haystack = normalize_text(f"{offer.category} {offer_record_product(offer) or ''} {offer.text}")
    best_slug = ""
    best_title = ""
    best_score = 0
    for categories, slug, title, keywords in MENU_TYPE_RULES:
        if category not in categories:
            continue
        score = sum(1 for keyword in keywords if keyword_in_normalized_text(keyword, haystack))
        if score > best_score:
            best_slug = slug
            best_title = title
            best_score = score
    if best_score:
        return best_slug, best_title
    fallback_titles = {
        "elettronica": "Altra elettronica",
        "giochi": "Altri giochi",
        "software": "Altro software",
        "casa": "Casa",
        "fai-da-te": "Fai-da-te",
        "bellezza": "Bellezza",
        "animali": "Animali",
    }
    return "altro", fallback_titles.get(category, category.replace("-", " ").title())


def offer_menu_key(offer: OfferRecord) -> str:
    slug, _title = offer_menu_type(offer)
    return f"{menu_category(offer.category)}:{slug}"


def menu_title_for_key(menu_key: str, offers: list[OfferRecord]) -> str:
    if offers:
        _slug, title = offer_menu_type(offers[0])
        return f"{menu_category(offers[0].category)} / {title}"
    category, _, slug = menu_key.partition(":")
    return f"{category} / {menu_slug_title(category, slug)}"


def grouped_active_offers(store: DedupeStore) -> dict[str, list[OfferRecord]]:
    groups: dict[str, list[OfferRecord]] = {}
    for offer in store.list_active_offers():
        if not passes_filters(store, offer.category, offer.price):
            continue
        groups.setdefault(offer_menu_key(offer), []).append(offer)
    return groups


def selected_menu_categories(store: DedupeStore) -> tuple[str, ...]:
    categories: list[str] = []
    for item in store.get_filter_categories():
        if item == "altro":
            continue
        category = menu_category(item)
        if category not in categories:
            categories.append(category)
    return tuple(categories)


def menu_seen_config_key(menu_key: str) -> str:
    return f"{MENU_SEEN_PREFIX}{menu_key}"


def menu_group_seen_at(store: DedupeStore, menu_key: str) -> int:
    value = store.get_config(menu_seen_config_key(menu_key)) or ""
    try:
        return max(0, int(value))
    except ValueError:
        return 0


def mark_menu_group_seen(store: DedupeStore, menu_key: str, seen_at: int | None = None) -> None:
    store.set_config(menu_seen_config_key(menu_key), str(seen_at or int(time.time())))


def menu_group_new_count(
    store: DedupeStore,
    menu_key: str,
    offers: Iterable[OfferRecord],
    now: int | None = None,
) -> int:
    current_time = now or int(time.time())
    seen_at = menu_group_seen_at(store, menu_key)
    new_count = 0
    for offer in offers:
        latest_at = store.offer_latest_source_at(offer.fingerprint)
        if latest_at > seen_at and latest_at >= current_time - MENU_NEW_WINDOW_SECONDS:
            new_count += 1
    return new_count


def render_menu_offer_line(store: DedupeStore, index: int, offer: OfferRecord, now: int) -> list[str]:
    latest_at = store.offer_latest_source_at(offer.fingerprint)
    is_new = latest_at >= now - MENU_NEW_WINDOW_SECONDS
    product = trim_text(offer_record_product(offer) or "Prodotto", 86)
    source_count = len(store.offer_source_messages(offer.fingerprint))
    price = format_price(offer.price) if offer.price is not None else "prezzo n/d"
    urls = offer_record_urls(offer)
    marker = "[NUOVA] " if is_new else ""
    source_text = f" | Fonti: {source_count}" if source_count > 1 else ""
    lines = [f"{marker}{index}. {product}", f"   {price}{source_text}"]
    if urls:
        lines.append(f"   {urls[0]}")
    return lines


def render_offer_menu_text(
    store: DedupeStore,
    menu_key: str,
    offers: list[OfferRecord],
    max_chars: int,
) -> str:
    now = int(time.time())
    title = menu_title_for_key(menu_key, offers)
    new_count = menu_group_new_count(store, menu_key, offers, now)
    updated_at = datetime.fromtimestamp(now).strftime("%d/%m/%Y %H:%M")
    lines = [
        f"Shopper Easly - {title}",
        f"Offerte attive: {len(offers)} | NUOVE 24h: {new_count}",
        f"Aggiornato: {updated_at}",
        "",
    ]
    for index, offer in enumerate(offers[:MENU_MAX_OFFERS], start=1):
        lines.extend(render_menu_offer_line(store, index, offer, now))
        lines.append("")
    remaining = len(offers) - MENU_MAX_OFFERS
    if remaining > 0:
        lines.append(f"... altre {remaining} offerte in questo gruppo.")
    return trim_text("\n".join(lines).strip(), max_chars)


def menu_group_summaries(store: DedupeStore) -> list[tuple[str, str, int, int]]:
    now = int(time.time())
    summaries = []
    groups = grouped_active_offers(store)
    for menu_key, offers in groups.items():
        title = menu_title_for_key(menu_key, offers)
        new_count = menu_group_new_count(store, menu_key, offers, now)
        summaries.append((menu_key, title, len(offers), new_count))
    covered_categories = {menu_key_parts(menu_key)[0] for menu_key in groups}
    for category in selected_menu_categories(store):
        if category in covered_categories:
            continue
        menu_key = f"{category}:all"
        summaries.append((menu_key, menu_title_for_key(menu_key, []), 0, 0))
    return sorted(summaries, key=lambda item: (item[1].lower(), item[0]))


def render_menu_index_text(store: DedupeStore) -> str:
    summaries = menu_group_summaries(store)
    total_offers = sum(total for _key, _title, total, _new_count in summaries)
    total_new = sum(new_count for _key, _title, _total, new_count in summaries)
    updated_at = datetime.fromtimestamp(int(time.time())).strftime("%d/%m/%Y %H:%M")
    lines = [
        "Shopper Easly - Menu offerte",
        f"Offerte attive: {total_offers} | NUOVE 24h: {total_new}",
        f"Aggiornato: {updated_at}",
        "",
    ]
    if not summaries:
        lines.append("Nessuna offerta attiva nei filtri correnti.")
    else:
        lines.append("Scegli una tipologia dai pulsanti qui sotto.")
        lines.append("[NUOVO] indica prodotti arrivati o aggiornati nelle ultime 24h.")
    return "\n".join(lines).strip()


def menu_key_parts(menu_key: str) -> tuple[str, str]:
    category, separator, slug = menu_key.partition(":")
    if not separator:
        return category, "altro"
    return category, slug


def menu_callback_data(action: str, menu_key: str, page: int = 0) -> bytes:
    category, slug = menu_key_parts(menu_key)
    return f"menu:{action}:{category}:{slug}:{max(0, page)}".encode("utf-8")[:64]


def parse_menu_callback_data(data: bytes) -> tuple[str, str, int] | None:
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    parts = decoded.split(":")
    if len(parts) < 2 or parts[0] != "menu":
        return None
    action = parts[1]
    if action == "close":
        if len(parts) == 2:
            return action, "", 0
        if len(parts) != 5:
            return None
    elif action != "open" or len(parts) != 5:
        return None
    try:
        page = max(0, int(parts[4]))
    except ValueError:
        page = 0
    return action, f"{parts[2]}:{parts[3]}", page


def menu_index_buttons(store: DedupeStore) -> list[list[Button]]:
    buttons = []
    for menu_key, title, total, new_count in menu_group_summaries(store):
        label = f"{'[NUOVO] ' if new_count else ''}{title} ({total})"
        buttons.append([Button.inline(label, menu_callback_data("open", menu_key, 0))])
    return buttons


def render_offer_menu_detail_text(
    store: DedupeStore,
    menu_key: str,
    offers: list[OfferRecord],
    page: int,
    max_chars: int,
) -> str:
    title = menu_title_for_key(menu_key, offers)
    now = int(time.time())
    new_count = menu_group_new_count(store, menu_key, offers, now)
    if not offers:
        lines = [
            f"Shopper Easly - {title}",
            "Offerte attive: 0 | NUOVE 24h: 0",
            "",
            "Nessuna offerta attiva in questa tipologia.",
        ]
        return trim_text("\n".join(lines).strip(), max_chars)
    page_count = max(1, (len(offers) + MENU_DETAIL_PAGE_SIZE - 1) // MENU_DETAIL_PAGE_SIZE)
    page = min(max(0, page), page_count - 1)
    start = page * MENU_DETAIL_PAGE_SIZE
    page_offers = offers[start : start + MENU_DETAIL_PAGE_SIZE]
    lines = [
        f"Shopper Easly - {title}",
        f"Offerte attive: {len(offers)} | NUOVE 24h: {new_count}",
        f"Pagina {page + 1}/{page_count}",
        "",
    ]
    for index, offer in enumerate(page_offers, start=start + 1):
        lines.extend(render_menu_offer_line(store, index, offer, now))
        lines.append("")
    return trim_text("\n".join(lines).strip(), max_chars)


def menu_detail_buttons(menu_key: str, page: int, offer_count: int) -> list[list[Button]]:
    page_count = max(1, (offer_count + MENU_DETAIL_PAGE_SIZE - 1) // MENU_DETAIL_PAGE_SIZE)
    page = min(max(0, page), page_count - 1)
    nav = []
    if page > 0:
        nav.append(Button.inline("Indietro", menu_callback_data("open", menu_key, page - 1)))
    if page + 1 < page_count:
        nav.append(Button.inline("Avanti", menu_callback_data("open", menu_key, page + 1)))
    rows = []
    if nav:
        rows.append(nav)
    rows.append([Button.inline("Chiudi", b"menu:close")])
    return rows


def open_menu_storage_key(menu_key: str) -> str:
    return f"{MENU_OPEN_PREFIX}{menu_key}"


def menu_expansion_close_buttons(menu_key: str) -> list[list[Button]]:
    return [[Button.inline("Chiudi", menu_callback_data("close", menu_key, 0))]]
