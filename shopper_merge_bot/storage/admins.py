from __future__ import annotations

import time


class AdminStoreMixin:


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
