from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


PRICE_RE = re.compile(
    r"(?:(?:eur|euro|€)\s*)?(\d{1,5}(?:[.,]\d{1,2})?)\s*(?:€|eur|euro)?",
    re.IGNORECASE,
)

INVALID_PATTERNS = (
    r"\bscadut[aoei]?\b",
    r"\bterminat[aoei]?\b",
    r"\besaurit[aoei]?\b",
    r"\bsold\s*out\b",
    r"\bexpired\b",
    r"non\s+(?:piu\s+)?disponibile",
    r"offerta\s+(?:non\s+)?(?:piu\s+)?valida",
    r"coupon\s+(?:non\s+)?(?:piu\s+)?valido",
    r"prezzo\s+(?:salito|aumentato|cambiato)",
)

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "elettronica": (
        "smartphone",
        "telefono",
        "iphone",
        "samsung",
        "xiaomi",
        "tablet",
        "pc",
        "notebook",
        "laptop",
        "monitor",
        "ssd",
        "hard disk",
        "router",
        "cuffie",
        "auricolari",
        "speaker",
        "bluetooth",
        "tv",
        "televisore",
        "fotocamera",
        "camera",
        "console",
        "playstation",
        "xbox",
        "nintendo",
    ),
    "casa": (
        "casa",
        "cucina",
        "friggitrice",
        "air fryer",
        "aspirapolvere",
        "robot",
        "lavatrice",
        "lavastoviglie",
        "materasso",
        "lenzuola",
        "divano",
        "sedia",
        "lampada",
        "giardino",
        "bricolage",
        "utensile",
        "trapano",
        "bosch",
    ),
    "moda": (
        "scarpe",
        "sneaker",
        "maglia",
        "felpa",
        "giacca",
        "jeans",
        "borsa",
        "zaino",
        "orologio",
        "abbigliamento",
        "moda",
        "vestito",
    ),
    "bellezza": (
        "beauty",
        "bellezza",
        "crema",
        "profumo",
        "rasoio",
        "spazzolino",
        "shampoo",
        "cosmetico",
        "makeup",
        "trimmer",
    ),
    "sport": (
        "sport",
        "fitness",
        "palestra",
        "bicicletta",
        "bike",
        "tapis roulant",
        "running",
        "trekking",
        "calcio",
        "padel",
    ),
    "giochi": (
        "lego",
        "gioco",
        "giochi",
        "giocattolo",
        "boardgame",
        "bambini",
        "puzzle",
        "nerf",
    ),
    "libri": (
        "libro",
        "libri",
        "kindle",
        "ebook",
        "audible",
        "fumetto",
        "manga",
    ),
    "auto": (
        "auto",
        "moto",
        "casco",
        "dashcam",
        "pneumatici",
        "olio motore",
        "avviatore",
    ),
    "alimentari": (
        "caffe",
        "caffè",
        "pasta",
        "vino",
        "olio",
        "cioccolato",
        "snack",
        "alimentari",
        "food",
        "bevanda",
    ),
    "software": (
        "software",
        "vpn",
        "licenza",
        "windows",
        "office",
        "antivirus",
        "abbonamento",
        "app",
    ),
}

OFFER_SOURCE_KEYWORDS = (
    "offerte",
    "offerta",
    "sconti",
    "sconto",
    "coupon",
    "codici sconto",
    "deal",
    "deals",
    "prezzo",
    "amazon",
    "shopping",
    "promo",
    "promozioni",
    "risparmio",
    "bottega",
    "shark",
    "junction",
    "gizchina",
    "mercatino",
)

NEWS_SOURCE_KEYWORDS = (
    "news",
    "notizie",
    "breaking",
    "ansa",
    "ultimora",
    "giornale",
    "quotidiano",
    "informazione",
)


@dataclass(frozen=True)
class OfferFacts:
    category: str
    price: Decimal | None
    invalid: bool


def _normalize_decimal(value: str) -> Decimal | None:
    cleaned = value.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def extract_price(text: str) -> Decimal | None:
    prices = []
    for match in PRICE_RE.finditer(text):
        raw = match.group(0).lower()
        if "€" not in raw and "eur" not in raw and "euro" not in raw:
            continue
        price = _normalize_decimal(match.group(1))
        if price is not None and price > 0:
            prices.append(price)
    return min(prices) if prices else None


def classify_category(text: str) -> str:
    normalized = text.lower()
    best_category = "altro"
    best_score = 0
    for category, keywords in CATEGORY_KEYWORDS.items():
        score = sum(1 for keyword in keywords if keyword in normalized)
        if score > best_score:
            best_category = category
            best_score = score
    return best_category


def is_invalid_offer(text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in INVALID_PATTERNS)


def analyze_offer(text: str) -> OfferFacts:
    return OfferFacts(
        category=classify_category(text),
        price=extract_price(text),
        invalid=is_invalid_offer(text),
    )


def source_score(title: str, username: str, mode: str = "offerte") -> int:
    haystack = f"{title} {username}".lower()
    keywords = NEWS_SOURCE_KEYWORDS if mode == "notizie" else OFFER_SOURCE_KEYWORDS
    score = sum(2 for keyword in keywords if keyword in haystack)
    if username.lower().endswith("bot"):
        score += 1
    return score


def parse_price_limit(value: str) -> Decimal | None:
    return _normalize_decimal(value.strip().replace("€", "").replace("eur", ""))
