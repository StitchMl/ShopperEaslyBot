from __future__ import annotations


def trim_text(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text

    return text[: max(0, limit - 4)].rstrip() + " ..."


def build_outbound_text(
    *,
    source_title: str,
    body: str,
    source_link: str | None,
    max_chars: int,
) -> str:
    parts = []
    if source_title:
        parts.append(f"Da: {source_title}")
    if body.strip():
        parts.append(body.strip())
    if source_link:
        parts.append(f"Fonte: {source_link}")

    return trim_text("\n\n".join(parts).strip(), max_chars)
