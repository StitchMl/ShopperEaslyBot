from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceChat:
    peer_id: str
    title: str
    username: str
    added_at: int


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
