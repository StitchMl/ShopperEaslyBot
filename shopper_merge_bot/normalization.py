from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)
TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_",
    "spm",
}


def extract_urls(text: str) -> list[str]:
    return [match.group(0).rstrip(".,;:!?]})") for match in URL_RE.finditer(text)]


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"

    query_items = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_PARAMS:
            continue
        query_items.append((lowered, value))

    query = urlencode(sorted(query_items), doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_text(text: str) -> str:
    without_urls = URL_RE.sub(" ", text)
    ascii_text = (
        unicodedata.normalize("NFKD", without_urls)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    lowered = ascii_text.lower()
    collapsed = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", collapsed).strip()


def build_fingerprint(text: str, fallback: str) -> str:
    urls = sorted({canonicalize_url(url) for url in extract_urls(text)})
    normalized = normalize_text(text)

    if urls:
        material = "urls:" + "|".join(urls[:8])
        if normalized:
            material += "|text:" + normalized[:600]
    elif normalized:
        material = "text:" + normalized[:900]
    else:
        material = "fallback:" + fallback

    return hashlib.sha256(material.encode("utf-8")).hexdigest()
