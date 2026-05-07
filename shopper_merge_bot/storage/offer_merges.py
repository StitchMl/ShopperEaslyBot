from __future__ import annotations

import sqlite3
import time


class OfferMergeStoreMixin:


    def rename_offer_fingerprint(self, old_fingerprint: str, new_fingerprint: str) -> bool:
        try:
            self._conn.execute("BEGIN")
            self._conn.execute(
                "UPDATE offers SET fingerprint = ?, updated_at = ? WHERE fingerprint = ?",
                (new_fingerprint, int(time.time()), old_fingerprint),
            )
            self._conn.execute(
                "UPDATE offer_sources SET fingerprint = ? WHERE fingerprint = ?",
                (new_fingerprint, old_fingerprint),
            )
            self._conn.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            self._conn.execute("ROLLBACK")
            return False

    def merge_offer_into(self, source_fingerprint: str, target_fingerprint: str) -> int:
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
            SELECT
                ?,
                source_chat_id,
                source_message_id,
                source_title,
                source_link,
                added_at
            FROM offer_sources
            WHERE fingerprint = ?
            """,
            (target_fingerprint, source_fingerprint),
        )
        source_count = self._conn.execute(
            "SELECT COUNT(*) FROM offer_sources WHERE fingerprint = ?",
            (target_fingerprint,),
        ).fetchone()[0]
        self._conn.execute(
            """
            UPDATE offers
            SET source_count = ?, updated_at = ?
            WHERE fingerprint = ?
            """,
            (int(source_count), int(time.time()), target_fingerprint),
        )
        self.mark_offer_status(source_fingerprint, "deleted:merged-duplicate")
        return int(source_count)
