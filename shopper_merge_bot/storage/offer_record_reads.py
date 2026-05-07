from __future__ import annotations

from decimal import Decimal

from .models import OfferRecord


class OfferRecordReadStoreMixin:


    def get_offer(self, fingerprint: str) -> OfferRecord | None:
        row = self._conn.execute(
            """
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
            WHERE fingerprint = ?
            """,
            (fingerprint,),
        ).fetchone()
        if row is None:
            return None
        extra_ids = tuple(
            int(item) for item in str(row[3]).split(",") if item.strip().isdigit()
        )
        return OfferRecord(
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

    def list_offers(self, status: str | None = "active") -> list[OfferRecord]:
        where = "WHERE status = ?" if status is not None else ""
        params = (status,) if status is not None else ()
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
            {where}
            ORDER BY updated_at DESC
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

    def list_active_offers(self) -> list[OfferRecord]:
        return self.list_offers("active")
