from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .normalization import canonicalize_url, extract_urls


PRICE_RE = re.compile(
    r"(?:(?:eur|euro|\u20ac)\s*)?(\d{1,5}(?:[.,]\d{1,2})?)\s*(?:\u20ac|eur|euro)?",
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
        "caff",
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
    product: str | None = None
    original_price: Decimal | None = None
    offer_url: str | None = None

    @property
    def current_price(self) -> Decimal | None:
        return self.price

    @property
    def complete(self) -> bool:
        return all(
            (
                self.product,
                self.original_price is not None,
                self.current_price is not None,
                self.offer_url,
            )
        )


def _normalize_decimal(value: str) -> Decimal | None:
    cleaned = value.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def extract_prices(text: str) -> list[Decimal]:
    prices = []
    for match in PRICE_RE.finditer(text):
        raw = match.group(0).lower()
        if "\u20ac" not in raw and "eur" not in raw and "euro" not in raw:
            continue
        price = _normalize_decimal(match.group(1))
        if price is not None and price > 0:
            prices.append(price)
    return prices


def extract_price(text: str) -> Decimal | None:
    prices = extract_prices(text)
    return min(prices) if prices else None


def extract_price_pair(text: str) -> tuple[Decimal | None, Decimal | None]:
    prices = extract_prices(text)
    unique_prices: list[Decimal] = []
    for price in prices:
        if price not in unique_prices:
            unique_prices.append(price)
    if len(unique_prices) < 2:
        return None, unique_prices[0] if unique_prices else None
    return max(unique_prices), min(unique_prices)


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


def is_offer_url(url: str) -> bool:
    lowered = url.lower()
    if "t.me/" in lowered or "telegram.me/" in lowered:
        return False
    if lowered.startswith("tg:"):
        return False
    return lowered.startswith("http://") or lowered.startswith("https://")


def pick_offer_url(text: str, urls: tuple[str, ...] = ()) -> str | None:
    candidates = [*urls, *extract_urls(text)]
    for url in candidates:
        if is_offer_url(url):
            return canonicalize_url(url)
    return None


def strip_noise(text: str) -> str:
    without_urls = re.sub(r"https?://\S+", " ", text)
    without_prices = PRICE_RE.sub(" ", without_urls)
    cleaned = re.sub(r"[^\w\s.,:/+-]", " ", without_prices, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()


def extract_product(text: str) -> str | None:
    banned_fragments = (
        "iscriviti",
        "gratis",
        "canale",
        "telegram",
        "fonte",
        "coupon",
        "codice",
        "prezzo",
        "amazon",
        "link",
        "categoria",
    )
    best_line = ""
    for line in text.splitlines():
        candidate = strip_noise(line)
        lowered = candidate.lower()
        if len(candidate) < 8:
            continue
        if any(fragment in lowered for fragment in banned_fragments):
            continue
        if len(candidate) > len(best_line):
            best_line = candidate

    if not best_line:
        candidate = strip_noise(text)
        if len(candidate) < 8:
            return None
        best_line = candidate

    return best_line[:140].strip(" -:,.") or None


def analyze_offer(text: str, urls: tuple[str, ...] = ()) -> OfferFacts:
    original_price, current_price = extract_price_pair(text)
    return OfferFacts(
        category=classify_category(text),
        price=current_price,
        invalid=is_invalid_offer(text),
        product=extract_product(text),
        original_price=original_price,
        offer_url=pick_offer_url(text, urls),
    )


def source_score(
    title: str,
    username: str,
    mode: str = "offerte",
    source_type: str = "chat",
) -> int:
    haystack = f"{title} {username} {source_type}".lower()
    keywords = NEWS_SOURCE_KEYWORDS if mode == "notizie" else OFFER_SOURCE_KEYWORDS
    score = sum(2 for keyword in keywords if keyword in haystack)
    if username.lower().endswith("bot"):
        score += 1
    if source_type in {"channel", "bot"}:
        score += 1
    return score


def parse_price_limit(value: str) -> Decimal | None:
    return _normalize_decimal(value.strip().replace("\u20ac", "").replace("eur", ""))
