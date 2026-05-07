from __future__ import annotations

import time

from .models import OfferSource


class OfferSourceStoreMixin:


    def add_offer_source(
        self,
        *,
        fingerprint: str,
        source_chat_id: str,
        source_message_id: int,
        source_title: str,
        source_link: str,
    ) -> bool:
        before = self._conn.total_changes
        self._conn.execute(
            """
            INSERT OR IGNORE INTO offer_sources(
                fingerprint,
                source_chat_id,
                source_message_id,
                source_title,
                source_link,
                added_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint,
                source_chat_id,
                source_message_id,
                source_title,
                source_link,
                int(time.time()),
            ),
        )
        return self._conn.total_changes > before

    def offer_sources(self, fingerprint: str) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            """
            SELECT source_title, source_link
            FROM offer_sources
            WHERE fingerprint = ?
            ORDER BY added_at
            """,
            (fingerprint,),
        ).fetchall()
        return [(str(title), str(link)) for title, link in rows]

    def offer_source_messages(self, fingerprint: str) -> list[OfferSource]:
        rows = self._conn.execute(
            """
            SELECT source_chat_id, source_message_id, source_title, source_link, added_at
            FROM offer_sources
            WHERE fingerprint = ?
            ORDER BY added_at
            """,
            (fingerprint,),
        ).fetchall()
        return [
            OfferSource(
                source_chat_id=str(row[0]),
                source_message_id=int(row[1]),
                source_title=str(row[2]),
                source_link=str(row[3]),
                added_at=int(row[4]),
            )
            for row in rows
        ]

    def offer_latest_source_at(self, fingerprint: str) -> int:
        row = self._conn.execute(
            """
            SELECT MAX(added_at)
            FROM offer_sources
            WHERE fingerprint = ?
            """,
            (fingerprint,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def fingerprints_for_source_message(
        self,
        source_chat_id: str,
        source_message_id: int,
    ) -> list[str]:
        rows = self._conn.execute(
            """
            SELECT fingerprint
            FROM offer_sources
            WHERE source_chat_id = ? AND source_message_id = ?
            """,
            (source_chat_id, source_message_id),
        ).fetchall()
        return [str(row[0]) for row in rows]
