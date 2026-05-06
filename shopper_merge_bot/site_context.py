from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MAX_HTML_BYTES = 700_000
FETCH_TIMEOUT_SECONDS = 4.0
SKIP_FETCH_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".avif",
    ".mp4",
    ".webm",
    ".pdf",
)
BOOK_ONLY_HOST_HINTS = (
    "ibs.it",
    "lafeltrinelli.it",
    "mondadoristore.it",
    "libraccio.it",
    "hoepli.it",
    "bookdealer.it",
)
AMAZON_HOST_HINTS = (
    "amazon.it",
    "amazon.com",
    "amazon.de",
    "amazon.fr",
    "amazon.es",
)


@dataclass(frozen=True)
class SiteContext:
    url: str
    host: str
    text: str
    fetched: bool


@dataclass(frozen=True)
class OfferActivity:
    url: str
    status: str
    reason: str
    fetched: bool
    current_price: Decimal | None = None


INACTIVE_HTTP_STATUS_CODES = {404, 410}
PRICE_CHANGE_MIN_DELTA = Decimal("0.50")
PRICE_CHANGE_RELATIVE_DELTA = Decimal("0.03")
ACTIVE_AVAILABILITY_PATTERNS = (
    r"\binstock\b",
    r"\bin stock\b",
    r"\bpreorder\b",
    r"\bpre order\b",
    r"\bavailable\b",
    r"\bdisponibile\b",
    r"\baggiungi al carrello\b",
    r"\bacquista ora\b",
    r"\badd to cart\b",
    r"\bbuy now\b",
)
INACTIVE_AVAILABILITY_PATTERNS = (
    r"\boutofstock\b",
    r"\bout of stock\b",
    r"\bsoldout\b",
    r"\bsold out\b",
    r"\bcurrently unavailable\b",
    r"\btemporarily unavailable\b",
    r"\bnot available\b",
    r"\bnot currently available\b",
    r"\bno longer available\b",
    r"\bunavailable\b",
    r"\bdiscontinued\b",
    r"\bexpired\b",
    r"\bdeal ended\b",
    r"\boffer expired\b",
    r"\bno featured offers available\b",
    r"\bnon disponibile\b",
    r"\bnon piu disponibile\b",
    r"\battualmente non disponibile\b",
    r"\btemporaneamente non disponibile\b",
    r"\bnessuna offerta in evidenza disponibile\b",
    r"\besaurit[oaie]?\b",
    r"\bterminat[oaie]?\b",
    r"\bscadut[oaie]?\b",
    r"offerta\s+(?:terminata|scaduta|finita|esaurita|non piu valida)",
    r"coupon\s+(?:scaduto|non valido)",
    r"prodotto\s+non\s+trovato",
    r"pagina\s+non\s+trovata",
    r"page\s+not\s+found",
    r"product\s+not\s+found",
)
ACTIVITY_ATTR_RE = re.compile(
    r"(?:availability|stock|unavailable|sold-?out|buybox|cart|deal|promo|cta)",
    flags=re.I,
)
PRICE_ATTR_RE = re.compile(
    r"(?:price|prezzo|deal|buybox|corePrice|a-price|offer|olp)",
    flags=re.I,
)
PRICE_WITH_CURRENCY_RE = re.compile(
    r"(?:€\s*\d{1,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|\d{1,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?\s*(?:€|eur|euro))",
    flags=re.I,
)
OFFER_PRICE_PREFIX_RE = re.compile(
    r"(?:acquista\s+nuovo|buy\s+new|prezzo|price).{0,80}?"
    r"(€\s*\d{1,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|\d{1,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?\s*(?:€|eur|euro))",
    flags=re.I | re.S,
)
ACTIVITY_SNIPPET_RE = re.compile(
    r"(?:attualmente\s+non\s+disponibile|temporaneamente\s+non\s+disponibile|"
    r"nessuna\s+offerta\s+in\s+evidenza\s+disponibile|"
    r"currently\s+unavailable|temporarily\s+unavailable|no\s+featured\s+offers\s+available|"
    r"disponibilit[àa]\s*:\s*solo\s+\d+|disponibilit[àa]\s+immediata)",
    flags=re.I,
)


def path_words(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    material = " ".join(
        item
        for item in re.split(r"[/_.+-]+", parts.path)
        if item and not re.fullmatch(r"[A-Z0-9]{10}", item, flags=re.IGNORECASE)
    )
    return re.sub(r"\s+", " ", material).strip()


def should_fetch_url(url: str) -> bool:
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return False
    return not path.endswith(SKIP_FETCH_EXTENSIONS)


def host_category_hint(host: str) -> str:
    host = host.lower().removeprefix("www.")
    if any(host == hint or host.endswith(f".{hint}") for hint in BOOK_ONLY_HOST_HINTS):
        return "libri book books"
    return ""


def url_category_hint(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path
    if any(host == hint or host.endswith(f".{hint}") for hint in AMAZON_HOST_HINTS):
        match = re.search(r"/(?:dp|gp/product)/([0-9][0-9x]{9})(?:[/?#]|$)", path, flags=re.I)
        if match:
            return "libri book books isbn"
    return ""


def amazon_mobile_url(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = parts.netloc.lower().removeprefix("www.")
    if not any(host == hint or host.endswith(f".{hint}") for hint in AMAZON_HOST_HINTS):
        return None
    if re.search(r"/gp/aw/d/[A-Z0-9]{10}(?:[/?#]|$)", parts.path, flags=re.I):
        return None
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?#]|$)", parts.path, flags=re.I)
    if not match:
        return None
    return f"https://www.{host}/gp/aw/d/{match.group(1).upper()}"


def strip_tags(value: str) -> str:
    without_scripts = re.sub(r"<(?:script|style)\b.*?</(?:script|style)>", " ", value, flags=re.I | re.S)
    without_tags = re.sub(r"<[^>]+>", " ", without_scripts)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def compact_text(parts: list[str]) -> str:
    cleaned = []
    seen = set()
    for part in parts:
        value = re.sub(r"\s+", " ", html.unescape(part or "")).strip()
        if not value:
            continue
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        cleaned.append(value)
    return " | ".join(cleaned)[:6000]


def parse_price_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        raw = str(value)
    else:
        raw = strip_tags(str(value))
    cleaned = (
        html.unescape(raw)
        .lower()
        .replace("\xa0", " ")
        .replace("€", "")
        .replace("eur", "")
        .replace("euro", "")
        .strip()
    )
    match = re.search(r"\d{1,6}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|\d{1,6}", cleaned)
    if not match:
        return None
    number = match.group(0)
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        number = number.replace(".", "").replace(",", ".")
    try:
        price = Decimal(number)
    except InvalidOperation:
        return None
    if price <= 0 or price > Decimal("100000"):
        return None
    return price.quantize(Decimal("0.01"))


def dedupe_prices(values: list[Decimal]) -> list[Decimal]:
    deduped: list[Decimal] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped


def normalize_spaced_price_text(value: str) -> str:
    return re.sub(
        r"(\d{1,6})\s+([,.])\s+(\d{1,2})(\s*(?:€|eur|euro))",
        r"\1\2\3\4",
        value,
        flags=re.I,
    )


def extract_prices_from_text(value: str) -> list[Decimal]:
    value = normalize_spaced_price_text(value)
    prices = []
    for match in PRICE_WITH_CURRENCY_RE.finditer(value):
        price = parse_price_decimal(match.group(0))
        if price is not None:
            prices.append(price)
    return dedupe_prices(prices)


def extract_jsonld_context(document: str) -> list[str]:
    contexts: list[str] = []
    for match in re.finditer(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        document,
        flags=re.I | re.S,
    ):
        raw = html.unescape(match.group(1)).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        contexts.extend(jsonld_values(payload))
    return contexts


def jsonld_values(payload: object) -> list[str]:
    values: list[str] = []
    if isinstance(payload, list):
        for item in payload:
            values.extend(jsonld_values(item))
        return values
    if not isinstance(payload, dict):
        return values

    node_type = payload.get("@type")
    types = {str(item).lower() for item in node_type} if isinstance(node_type, list) else {str(node_type).lower()}
    if "breadcrumblist" in types:
        for item in payload.get("itemListElement", []):
            if isinstance(item, dict):
                value = item.get("name")
                if isinstance(value, str):
                    values.append(value)
                nested = item.get("item")
                if isinstance(nested, dict) and isinstance(nested.get("name"), str):
                    values.append(str(nested["name"]))
    if "product" in types:
        for key in ("name", "category", "description"):
            value = payload.get(key)
            if isinstance(value, str):
                values.append(value)
    if "@graph" in payload:
        values.extend(jsonld_values(payload["@graph"]))
    return values


def jsonld_price_values(payload: object) -> list[Decimal]:
    values: list[Decimal] = []
    if isinstance(payload, list):
        for item in payload:
            values.extend(jsonld_price_values(item))
        return values
    if not isinstance(payload, dict):
        return values

    for key, value in payload.items():
        lowered = str(key).lower()
        if lowered in {"price", "lowprice", "highprice", "saleprice"}:
            price = parse_price_decimal(value)
            if price is not None:
                values.append(price)
        elif isinstance(value, (dict, list)):
            values.extend(jsonld_price_values(value))
    return values


def extract_jsonld_prices(document: str) -> list[Decimal]:
    values: list[Decimal] = []
    for match in re.finditer(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        document,
        flags=re.I | re.S,
    ):
        raw = html.unescape(match.group(1)).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        values.extend(jsonld_price_values(payload))
    return dedupe_prices(values)


def flatten_jsonld_text(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(flatten_jsonld_text(item))
        return values
    if isinstance(value, dict):
        values = []
        for key in ("@id", "id", "url", "name", "value"):
            item = value.get(key)
            if isinstance(item, str):
                values.append(item)
        return values
    return []


def jsonld_availability_values(payload: object) -> list[str]:
    values: list[str] = []
    if isinstance(payload, list):
        for item in payload:
            values.extend(jsonld_availability_values(item))
        return values
    if not isinstance(payload, dict):
        return values

    for key, value in payload.items():
        lowered = str(key).lower()
        if lowered in {"availability", "itemavailability"}:
            values.extend(flatten_jsonld_text(value))
        elif isinstance(value, (dict, list)):
            values.extend(jsonld_availability_values(value))
    return values


def extract_jsonld_availability(document: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        document,
        flags=re.I | re.S,
    ):
        raw = html.unescape(match.group(1)).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        values.extend(jsonld_availability_values(payload))
    return values


def extract_meta_context(document: str) -> list[str]:
    contexts: list[str] = []
    for match in re.finditer(r"<meta\b([^>]+)>", document, flags=re.I):
        attrs = match.group(1)
        name_match = re.search(r"(?:name|property|itemprop)=[\"']([^\"']+)[\"']", attrs, flags=re.I)
        content_match = re.search(r"content=[\"']([^\"']+)[\"']", attrs, flags=re.I)
        if not name_match or not content_match:
            continue
        name = name_match.group(1).lower()
        if name in {"keywords", "description", "og:title", "product:category", "article:section", "category"}:
            contexts.append(content_match.group(1))
    title_match = re.search(r"<title[^>]*>(.*?)</title>", document, flags=re.I | re.S)
    if title_match:
        contexts.append(strip_tags(title_match.group(1)))
    return contexts


def extract_meta_availability(document: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(r"<meta\b([^>]+)>", document, flags=re.I):
        attrs = match.group(1)
        name_match = re.search(r"(?:name|property|itemprop)=[\"']([^\"']+)[\"']", attrs, flags=re.I)
        content_match = re.search(r"content=[\"']([^\"']+)[\"']", attrs, flags=re.I)
        if not name_match or not content_match:
            continue
        name = name_match.group(1).lower()
        if "availability" in name or name in {"product:status", "og:availability"}:
            values.append(content_match.group(1))
    return values


def extract_meta_prices(document: str) -> list[Decimal]:
    prices: list[Decimal] = []
    for match in re.finditer(r"<meta\b([^>]+)>", document, flags=re.I):
        attrs = match.group(1)
        name_match = re.search(r"(?:name|property|itemprop)=[\"']([^\"']+)[\"']", attrs, flags=re.I)
        content_match = re.search(r"content=[\"']([^\"']+)[\"']", attrs, flags=re.I)
        if not name_match or not content_match:
            continue
        name = name_match.group(1).lower()
        if "price" not in name and name not in {"amount", "product:amount"}:
            continue
        price = parse_price_decimal(content_match.group(1))
        if price is not None:
            prices.append(price)
    return dedupe_prices(prices)


def extract_breadcrumb_context(document: str) -> list[str]:
    contexts: list[str] = []
    for match in re.finditer(
        r"<(?:nav|ul|ol|div)[^>]+(?:breadcrumb|breadcrumbs|wayfinding)[^>]*>(.*?)</(?:nav|ul|ol|div)>",
        document,
        flags=re.I | re.S,
    ):
        text = strip_tags(match.group(1))
        if text:
            contexts.append(text)
    return contexts[:8]


def normalize_activity_text(value: str) -> str:
    cleaned = strip_tags(value).lower().replace("\u00f9", "u")
    cleaned = re.sub(r"[_-]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def activity_status_from_text(value: str) -> str | None:
    normalized = normalize_activity_text(value)
    if not normalized:
        return None
    if any(re.search(pattern, normalized, flags=re.I) for pattern in INACTIVE_AVAILABILITY_PATTERNS):
        return "inactive"
    if any(re.search(pattern, normalized, flags=re.I) for pattern in ACTIVE_AVAILABILITY_PATTERNS):
        return "active"
    return None


def activity_status_from_values(values: list[str]) -> str | None:
    statuses = {
        status
        for value in values
        if (status := activity_status_from_text(value)) is not None
    }
    if len(statuses) == 1:
        return next(iter(statuses))
    if not statuses:
        return None
    return None


def extract_embedded_price_values(document: str) -> list[Decimal]:
    prices: list[Decimal] = []
    patterns = (
        r'"priceAmount"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        r'"displayPrice"\s*:\s*"([^"]+)"',
        r'"priceToPay"\s*:\s*\{[^{}]*"displayString"\s*:\s*"([^"]+)"',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, document, flags=re.I | re.S):
            price = parse_price_decimal(match.group(1))
            if price is not None:
                prices.append(price)
    return dedupe_prices(prices)


def extract_price_contexts(document: str) -> list[str]:
    contexts: list[str] = []
    for match in re.finditer(
        r"<(?P<tag>div|span|p|section|td)[^>]*(?P<attrs>(?:id|class|data-testid|aria-label|title)=[^>]*)[^>]*>(?P<body>.*?)</(?P=tag)>",
        document,
        flags=re.I | re.S,
    ):
        attrs = match.group("attrs")
        body = match.group("body")
        if PRICE_ATTR_RE.search(attrs):
            text = strip_tags(body)
            if text:
                contexts.append(text)
        for attr_value in re.findall(r"(?:aria-label|title)=[\"']([^\"']+)[\"']", attrs, flags=re.I):
            if PRICE_ATTR_RE.search(attr_value):
                contexts.append(attr_value)
    return contexts[:80]


def extract_visible_prices(document: str) -> list[Decimal]:
    prices: list[Decimal] = []
    for context in extract_price_contexts(document):
        prices.extend(extract_prices_from_text(context))
    return dedupe_prices(prices)


def extract_prefixed_offer_prices(document: str) -> list[Decimal]:
    text = normalize_spaced_price_text(strip_tags(document))
    prices = []
    for match in OFFER_PRICE_PREFIX_RE.finditer(text):
        price = parse_price_decimal(match.group(1))
        if price is not None:
            prices.append(price)
    return dedupe_prices(prices)


def extract_current_offer_price(document: str) -> Decimal | None:
    for extractor in (
        extract_jsonld_prices,
        extract_meta_prices,
        extract_embedded_price_values,
        extract_prefixed_offer_prices,
        extract_visible_prices,
    ):
        prices = extractor(document)
        if prices:
            return min(prices)
    return None


def current_price_exceeds_expected(current_price: Decimal, expected_price: Decimal) -> bool:
    allowed_delta = max(PRICE_CHANGE_MIN_DELTA, expected_price * PRICE_CHANGE_RELATIVE_DELTA)
    return current_price > expected_price + allowed_delta


def fetch_html(url: str, timeout_seconds: float = FETCH_TIMEOUT_SECONDS) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
            ),
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.6",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        content_type = response.headers.get_content_type().lower()
        if content_type not in {"text/html", "application/xhtml+xml", "application/ld+json"}:
            raise ValueError(f"unsupported content type: {content_type}")
        raw = response.read(MAX_HTML_BYTES)
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def offer_activity_from_document(
    url: str,
    document: str,
    expected_price: Decimal | None = None,
) -> OfferActivity:
    structured_values = [
        *extract_jsonld_availability(document),
        *extract_meta_availability(document),
    ]
    structured_status = activity_status_from_values(structured_values)
    context_status = activity_status_from_values(extract_activity_contexts(document))
    activity_status = structured_status or context_status
    activity_reason = "structured-availability" if structured_status else "page-availability"
    current_price = extract_current_offer_price(document)

    if activity_status == "inactive":
        return OfferActivity(
            url=url,
            status="inactive",
            reason=activity_reason,
            fetched=True,
            current_price=current_price,
        )

    if expected_price is not None and current_price is not None:
        if current_price_exceeds_expected(current_price, expected_price):
            return OfferActivity(
                url=url,
                status="inactive",
                reason=f"price-increased:{current_price}>{expected_price}",
                fetched=True,
                current_price=current_price,
            )
        return OfferActivity(
            url=url,
            status="active",
            reason="price-match",
            fetched=True,
            current_price=current_price,
        )

    if activity_status == "active":
        return OfferActivity(
            url=url,
            status="active",
            reason=activity_reason,
            fetched=True,
            current_price=current_price,
        )

    return OfferActivity(
        url=url,
        status="unknown",
        reason="no-availability-signal",
        fetched=True,
        current_price=current_price,
    )


def offer_activity_for_url(
    url: str,
    expected_price: Decimal | None = None,
    *,
    allow_amazon_mobile_fallback: bool = True,
) -> OfferActivity:
    try:
        host = urlsplit(url).netloc.lower().removeprefix("www.")
    except ValueError:
        host = ""
    if not host:
        return OfferActivity(url=url, status="unknown", reason="invalid-url", fetched=False)

    try:
        if not should_fetch_url(url):
            raise ValueError("non-html offer URL")
        document = fetch_html(url)
    except HTTPError as exc:
        if exc.code in INACTIVE_HTTP_STATUS_CODES:
            return OfferActivity(url=url, status="inactive", reason=f"http-{exc.code}", fetched=False)
        return OfferActivity(url=url, status="unknown", reason=f"http-{exc.code}", fetched=False)
    except (URLError, TimeoutError, ValueError, OSError) as exc:
        return OfferActivity(url=url, status="unknown", reason=exc.__class__.__name__, fetched=False)

    activity = offer_activity_from_document(url, document, expected_price)
    if (
        allow_amazon_mobile_fallback
        and activity.status == "unknown"
        and activity.current_price is None
        and (fallback_url := amazon_mobile_url(url)) is not None
    ):
        fallback = offer_activity_for_url(
            fallback_url,
            expected_price,
            allow_amazon_mobile_fallback=False,
        )
        if fallback.status != "unknown" or fallback.current_price is not None:
            return OfferActivity(
                url=url,
                status=fallback.status,
                reason=f"amazon-mobile:{fallback.reason}",
                fetched=fallback.fetched,
                current_price=fallback.current_price,
            )

    return activity


def extract_activity_contexts(document: str) -> list[str]:
    contexts: list[str] = []
    title_match = re.search(r"<title[^>]*>(.*?)</title>", document, flags=re.I | re.S)
    if title_match:
        contexts.append(title_match.group(1))

    for match in re.finditer(
        r"<(?P<tag>div|span|p|section|button)[^>]*(?P<attrs>(?:id|class|data-testid|aria-label|title)=[^>]*)[^>]*>(?P<body>.*?)</(?P=tag)>",
        document,
        flags=re.I | re.S,
    ):
        attrs = match.group("attrs")
        body = match.group("body")
        if ACTIVITY_ATTR_RE.search(attrs):
            text = strip_tags(body)
            if text:
                contexts.append(text)
        for attr_value in re.findall(r"(?:aria-label|title)=[\"']([^\"']+)[\"']", attrs, flags=re.I):
            if ACTIVITY_ATTR_RE.search(attr_value):
                contexts.append(attr_value)

    page_text = strip_tags(document)
    for match in ACTIVITY_SNIPPET_RE.finditer(page_text):
        start = max(0, match.start() - 120)
        end = min(len(page_text), match.end() + 120)
        contexts.append(page_text[start:end])

    return contexts[:60]


@lru_cache(maxsize=2048)
def site_context_for_url(url: str) -> SiteContext:
    try:
        host = urlsplit(url).netloc.lower().removeprefix("www.")
    except ValueError:
        host = ""
    parts = [host_category_hint(host), url_category_hint(url), path_words(url)]
    fetched = False
    try:
        if not should_fetch_url(url):
            raise ValueError("non-html offer URL")
        document = fetch_html(url)
        fetched = True
        parts.extend(extract_jsonld_context(document))
        parts.extend(extract_meta_context(document))
        parts.extend(extract_breadcrumb_context(document))
    except (HTTPError, URLError, TimeoutError, ValueError, OSError):
        pass
    return SiteContext(
        url=url,
        host=host,
        text=compact_text(parts),
        fetched=fetched,
    )


def combined_site_context(urls: tuple[str, ...]) -> str:
    contexts = []
    for url in urls[:3]:
        context = site_context_for_url(url)
        if context.text:
            contexts.append(context.text)
    return compact_text(contexts)
