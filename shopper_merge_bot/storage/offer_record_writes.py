from __future__ import annotations

import time
from decimal import Decimal


class OfferRecordWriteStoreMixin:


    def save_offer(
        self,
        *,
        fingerprint: str,
        destination_chat_id: str,
        primary_message_id: int,
        extra_message_ids: tuple[int, ...],
        text: str,
        category: str,
        price: Decimal | None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO offers(
                fingerprint,
                destination_chat_id,
                primary_message_id,
                extra_message_ids,
                text,
                category,
                price,
                source_count,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                destination_chat_id = excluded.destination_chat_id,
                primary_message_id = excluded.primary_message_id,
                extra_message_ids = excluded.extra_message_ids,
                text = excluded.text,
                category = excluded.category,
                price = excluded.price,
                source_count = 1,
                status = 'active',
                link_checked_at = NULL,
                link_status = 'unknown',
                updated_at = excluded.updated_at
            """,
            (
                fingerprint,
                destination_chat_id,
                primary_message_id,
                ",".join(str(item) for item in extra_message_ids),
                text,
                category,
                str(price) if price is not None else None,
                int(time.time()),
                int(time.time()),
            ),
        )

    def update_offer_text(self, fingerprint: str, text: str, source_count: int) -> None:
        self._conn.execute(
            """
            UPDATE offers
            SET text = ?, source_count = ?, updated_at = ?
            WHERE fingerprint = ?
            """,
            (text, source_count, int(time.time()), fingerprint),
        )

    def update_offer_delivery(
        self,
        fingerprint: str,
        primary_message_id: int,
        extra_message_ids: tuple[int, ...],
    ) -> None:
        self._conn.execute(
            """
            UPDATE offers
            SET primary_message_id = ?, extra_message_ids = ?, updated_at = ?
            WHERE fingerprint = ?
            """,
            (
                primary_message_id,
                ",".join(str(item) for item in extra_message_ids),
                int(time.time()),
                fingerprint,
            ),
        )

    def mark_offer_status(self, fingerprint: str, status: str) -> None:
        self._conn.execute(
            """
            UPDATE offers
            SET status = ?, updated_at = ?
            WHERE fingerprint = ?
            """,
            (status, int(time.time()), fingerprint),
        )

    def update_offer_category(self, fingerprint: str, category: str) -> None:
        self._conn.execute(
            """
            UPDATE offers
            SET category = ?, updated_at = ?
            WHERE fingerprint = ?
            """,
            (category, int(time.time()), fingerprint),
        )
