from __future__ import annotations

import sqlite3
import time


class SeenStoreMixin:


    def has_message(self, source_chat_id: str, message_id: int) -> bool:
        row = self._conn.execute(
            """
            SELECT 1
            FROM seen_messages
            WHERE source_chat_id = ? AND message_id = ?
            """,
            (source_chat_id, message_id),
        ).fetchone()
        return row is not None

    def mark_message(self, source_chat_id: str, message_id: int) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO seen_messages(source_chat_id, message_id, seen_at)
            VALUES (?, ?, ?)
            """,
            (source_chat_id, message_id, int(time.time())),
        )

    def claim_fingerprint(
        self,
        fingerprint: str,
        source_chat_id: str,
        message_id: int,
        ttl_seconds: int,
    ) -> bool:
        self.prune(ttl_seconds)
        try:
            self._conn.execute(
                """
                INSERT INTO fingerprints(
                    fingerprint,
                    first_seen_at,
                    source_chat_id,
                    message_id
                )
                VALUES (?, ?, ?, ?)
                """,
                (fingerprint, int(time.time()), source_chat_id, message_id),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def release_fingerprint(self, fingerprint: str) -> None:
        self._conn.execute(
            "DELETE FROM fingerprints WHERE fingerprint = ?",
            (fingerprint,),
        )

    def prune(self, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return

        cutoff = int(time.time()) - ttl_seconds
        self._conn.execute(
            "DELETE FROM fingerprints WHERE first_seen_at < ?",
            (cutoff,),
        )
        self._conn.execute(
            "DELETE FROM seen_messages WHERE seen_at < ?",
            (cutoff,),
        )
