from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

from .dedupe import DedupeStore, OfferRecord
from .formatter import trim_text
from .normalization import build_fingerprint, canonicalize_url, normalize_text
from .offer_analysis import parse_price_limit


def format_price(value: object) -> str:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return f"{value} EUR"
    return f"{amount:.2f}".replace(".", ",") + " EUR"


def savings_line(original_price: object, current_price: object) -> str | None:
    try:
        original = Decimal(str(original_price))
        current = Decimal(str(current_price))
    except (InvalidOperation, ValueError):
        return None
    if original <= 0 or current >= original:
        return None

    saving = (original - current).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    discount = ((saving / original) * Decimal("100")).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )
    return f"✅ Risparmio stimato: {format_price(saving)} ({discount}%)"


def stable_offer_body(*, product: str, original_price: object, current_price: object, offer_url: str) -> str:
    lines = [
        "🔥 Offerta pronta da controllare",
        "",
        f"📦 Prodotto: {product}",
        "",
        f"💸 Prezzo attuale: {format_price(current_price)}",
        f"🏷️ Prezzo originale: {format_price(original_price)}",
    ]
    saving = savings_line(original_price, current_price)
    if saving:
        lines.append(saving)
    lines.extend(["", f"🔗 Link offerta: {offer_url}"])
    return "\n".join(lines)


def ensure_offer_body_style(text: str) -> str:
    replacements = (
        ("Offerta pronta da controllare", "🔥 Offerta pronta da controllare"),
        ("Prodotto:", "📦 Prodotto:"),
        ("Prezzo attuale:", "💸 Prezzo attuale:"),
        ("Prezzo originale:", "🏷️ Prezzo originale:"),
        ("Risparmio stimato:", "✅ Risparmio stimato:"),
        ("Link offerta:", "🔗 Link offerta:"),
    )
    styled_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        for plain, styled in replacements:
            if stripped == plain or stripped.startswith(f"{plain} "):
                leading = line[: len(line) - len(line.lstrip())]
                line = leading + styled + stripped[len(plain) :]
                break
        styled_lines.append(line)
    return "\n".join(styled_lines)


def build_offer_publish_text_from_body(
    *,
    body: str,
    category: str,
    sources: list[tuple[str, str]],
    max_chars: int,
) -> str:
    text = ensure_offer_body_style(body)
    if category:
        text += f"\n\n📂 Categoria: {category}"
    if len(sources) > 1:
        text += f"\n🔁 Confermata da {len(sources)} fonti"
    return trim_text(text, max_chars)


def build_offer_publish_text(
    *,
    product: str,
    original_price: object,
    current_price: object,
    offer_url: str,
    category: str,
    sources: list[tuple[str, str]],
    max_chars: int,
) -> str:
    return build_offer_publish_text_from_body(
        body=stable_offer_body(
            product=product,
            original_price=original_price,
            current_price=current_price,
            offer_url=offer_url,
        ),
        category=category,
        sources=sources,
        max_chars=max_chars,
    )


def is_structured_offer_text(text: str) -> bool:
    required_labels = (
        "Prodotto:",
        "Prezzo originale:",
        "Prezzo attuale:",
        "Link offerta:",
    )
    return all(label in text for label in required_labels)


def fingerprint_for_offer_url(url: str) -> str:
    return build_fingerprint(canonicalize_url(url), fallback=url)


def fingerprints_from_urls(urls: Iterable[str]) -> set[str]:
    return {fingerprint_for_offer_url(url) for url in urls if url}


def strip_product_suffixes(product: str) -> str:
    cleaned = re.sub(
        r"\b(?:scende|sceso|sale|salito|torna)\b.*$",
        "",
        product,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:minimo storico|record minimo|offerta lampo|super prezzo)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", cleaned).strip(" -,:")


def product_similarity(left: str, right: str) -> float:
    left_tokens = set(normalize_text(strip_product_suffixes(left)).split())
    right_tokens = set(normalize_text(strip_product_suffixes(right)).split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))


def extract_offer_label(text: str, label: str) -> str | None:
    pattern = rf"(?:[^\w\s]|\ufe0f|\s)*{re.escape(label)}\s*(.+)"
    for line in text.splitlines():
        match = re.match(pattern, line.strip(), flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def offer_record_product(offer: OfferRecord) -> str | None:
    return extract_offer_label(offer.text, "Prodotto:")


def offer_record_original_price(offer: OfferRecord) -> Decimal | None:
    value = extract_offer_label(offer.text, "Prezzo originale:")
    return parse_price_limit(value.lower()) if value else None


def is_similar_offer(
    offer: OfferRecord,
    *,
    product: str,
    original_price: object,
    current_price: object,
) -> bool:
    if offer.price != current_price:
        return False
    existing_product = offer_record_product(offer)
    if not existing_product:
        return False
    if offer_record_original_price(offer) != original_price:
        return False
    return product_similarity(existing_product, product) >= 0.78


def find_similar_active_offer(
    store: DedupeStore,
    *,
    product: str,
    original_price: object,
    current_price: object,
    excluded_fingerprint: str,
) -> OfferRecord | None:
    for offer in store.list_active_offers():
        if offer.fingerprint == excluded_fingerprint:
            continue
        if is_similar_offer(
            offer,
            product=product,
            original_price=original_price,
            current_price=current_price,
        ):
            return offer
    return None


def offer_record_urls(offer: OfferRecord) -> tuple[str, ...]:
    return tuple(re.findall(r"https?://[^\s<>()\"']+", offer.text or ""))
