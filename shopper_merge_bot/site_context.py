from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
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
