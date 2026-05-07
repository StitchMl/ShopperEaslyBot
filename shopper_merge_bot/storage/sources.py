from __future__ import annotations

import time

from .models import SourceChat


class SourceStoreMixin:


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
