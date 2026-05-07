from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SourceChat:
    peer_id: str
    title: str
    username: str
    added_at: int


@dataclass(frozen=True)
class OfferRecord:
    fingerprint: str
    destination_chat_id: str
    primary_message_id: int
    extra_message_ids: tuple[int, ...]
    text: str
    category: str
    price: Decimal | None
    source_count: int
    status: str


@dataclass(frozen=True)
class OfferSource:
    source_chat_id: str
    source_message_id: int
    source_title: str
    source_link: str
    added_at: int


@dataclass(frozen=True)
class MenuMessage:
    menu_key: str
    message_id: int
    extra_message_ids: tuple[int, ...]
    title: str
    updated_at: int
