from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


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


class DedupeStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen_messages (
                source_chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                seen_at INTEGER NOT NULL,
                PRIMARY KEY (source_chat_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS fingerprints (
                fingerprint TEXT PRIMARY KEY,
                first_seen_at INTEGER NOT NULL,
                source_chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_chats (
                peer_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                added_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_admins (
                user_id INTEGER PRIMARY KEY,
                added_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS config_values (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS offers (
                fingerprint TEXT PRIMARY KEY,
                destination_chat_id TEXT NOT NULL,
                primary_message_id INTEGER NOT NULL,
                extra_message_ids TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'altro',
                price TEXT,
                source_count INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS offer_sources (
                fingerprint TEXT NOT NULL,
                source_chat_id TEXT NOT NULL,
                source_message_id INTEGER NOT NULL,
                source_title TEXT NOT NULL DEFAULT '',
                source_link TEXT NOT NULL DEFAULT '',
                added_at INTEGER NOT NULL,
                PRIMARY KEY (fingerprint, source_chat_id, source_message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_offer_sources_message
            ON offer_sources(source_chat_id, source_message_id);
            """
        )

    def get_config(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM config_values WHERE key = ?",
            (key,),
        ).fetchone()
        return str(row[0]) if row else None

    def set_config(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO config_values(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def add_source(self, peer_id: str, title: str = "", username: str = "") -> None:
        self._conn.execute(
            """
            INSERT INTO source_chats(peer_id, title, username, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(peer_id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username
            """,
            (peer_id, title, username, int(time.time())),
        )

    def remove_source(self, peer_id: str) -> None:
        self._conn.execute(
            "DELETE FROM source_chats WHERE peer_id = ?",
            (peer_id,),
        )

    def clear_sources(self) -> None:
        self._conn.execute("DELETE FROM source_chats")

    def list_sources(self) -> list[SourceChat]:
        rows = self._conn.execute(
            """
            SELECT peer_id, title, username, added_at
            FROM source_chats
            ORDER BY lower(title), peer_id
            """
        ).fetchall()
        return [
            SourceChat(
                peer_id=str(peer_id),
                title=str(title),
                username=str(username),
                added_at=int(added_at),
            )
            for peer_id, title, username, added_at in rows
        ]

    def source_ids(self) -> set[str]:
        return {source.peer_id for source in self.list_sources()}

    def has_admins(self) -> bool:
        row = self._conn.execute("SELECT 1 FROM bot_admins LIMIT 1").fetchone()
        return row is not None

    def add_admin(self, user_id: int) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO bot_admins(user_id, added_at)
            VALUES (?, ?)
            """,
            (user_id, int(time.time())),
        )

    def is_admin(self, user_id: int, configured_admin_ids: tuple[int, ...]) -> bool:
        if user_id in configured_admin_ids:
            return True
        row = self._conn.execute(
            "SELECT 1 FROM bot_admins WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row is not None

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
                text = excluded.text,
                category = excluded.category,
                price = excluded.price,
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

    def get_filter_categories(self) -> tuple[str, ...]:
        value = self.get_config("filter_categories") or ""
        return tuple(item.strip().lower() for item in value.split(",") if item.strip())

    def set_filter_categories(self, categories: tuple[str, ...]) -> None:
        self.set_config("filter_categories", ",".join(categories))

    def get_filter_price(self, key: str) -> Decimal | None:
        value = self.get_config(key)
        return Decimal(value) if value else None

    def set_filter_price(self, key: str, value: Decimal | None) -> None:
        self.set_config(key, str(value) if value is not None else "")

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
