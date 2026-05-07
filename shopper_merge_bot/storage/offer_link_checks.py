from __future__ import annotations

import time
from decimal import Decimal

from .models import OfferRecord


class OfferLinkCheckStoreMixin:


    def list_active_offers_for_link_check(self, limit: int) -> list[OfferRecord]:
        limit_clause = "LIMIT ?" if limit > 0 else ""
        params = (int(limit),) if limit > 0 else ()
        rows = self._conn.execute(
            f"""
            SELECT
                fingerprint,
                destination_chat_id,
                primary_message_id,
                extra_message_ids,
                text,
                category,
                price,
                source_count,
                status
            FROM offers
            WHERE status = 'active'
            ORDER BY COALESCE(link_checked_at, 0), updated_at DESC
            {limit_clause}
            """,
            params,
        ).fetchall()
        offers = []
        for row in rows:
            extra_ids = tuple(
                int(item) for item in str(row[3]).split(",") if item.strip().isdigit()
            )
            offers.append(
                OfferRecord(
                    fingerprint=str(row[0]),
                    destination_chat_id=str(row[1]),
                    primary_message_id=int(row[2]),
                    extra_message_ids=extra_ids,
                    text=str(row[4]),
                    category=str(row[5]),
                    price=Decimal(str(row[6])) if row[6] else None,
                    source_count=int(row[7]),
                    status=str(row[8]),
                )
            )
        return offers

    def mark_offer_link_check(self, fingerprint: str, status: str) -> None:
        self._conn.execute(
            """
            UPDATE offers
            SET link_checked_at = ?, link_status = ?
            WHERE fingerprint = ?
            """,
            (int(time.time()), status[:120], fingerprint),
        )
