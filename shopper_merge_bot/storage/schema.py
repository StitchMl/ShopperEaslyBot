from __future__ import annotations

class SchemaStoreMixin:


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
                category TEXT NOT NULL DEFAULT 'casa',
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

            CREATE TABLE IF NOT EXISTS menu_messages (
                menu_key TEXT PRIMARY KEY,
                message_id INTEGER NOT NULL,
                extra_message_ids TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            );
            """
        )
        self._ensure_column("offers", "link_checked_at", "INTEGER")
        self._ensure_column("offers", "link_status", "TEXT NOT NULL DEFAULT 'unknown'")

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        if any(str(row[1]) == column for row in rows):
            return
        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
