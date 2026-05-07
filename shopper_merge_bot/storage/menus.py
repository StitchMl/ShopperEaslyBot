from __future__ import annotations

import time

from .models import MenuMessage


class MenuStoreMixin:


    def get_menu_message(self, menu_key: str) -> MenuMessage | None:
        row = self._conn.execute(
            """
            SELECT menu_key, message_id, extra_message_ids, title, updated_at
            FROM menu_messages
            WHERE menu_key = ?
            """,
            (menu_key,),
        ).fetchone()
        if row is None:
            return None
        extra_ids = tuple(
            int(item) for item in str(row[2]).split(",") if item.strip().isdigit()
        )
        return MenuMessage(
            menu_key=str(row[0]),
            message_id=int(row[1]),
            extra_message_ids=extra_ids,
            title=str(row[3]),
            updated_at=int(row[4]),
        )

    def list_menu_messages(self) -> list[MenuMessage]:
        rows = self._conn.execute(
            """
            SELECT menu_key, message_id, extra_message_ids, title, updated_at
            FROM menu_messages
            ORDER BY lower(title), menu_key
            """
        ).fetchall()
        menus = []
        for row in rows:
            extra_ids = tuple(
                int(item) for item in str(row[2]).split(",") if item.strip().isdigit()
            )
            menus.append(
                MenuMessage(
                    menu_key=str(row[0]),
                    message_id=int(row[1]),
                    extra_message_ids=extra_ids,
                    title=str(row[3]),
                    updated_at=int(row[4]),
                )
            )
        return menus

    def save_menu_message(
        self,
        *,
        menu_key: str,
        message_id: int,
        extra_message_ids: tuple[int, ...] = (),
        title: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO menu_messages(menu_key, message_id, extra_message_ids, title, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(menu_key) DO UPDATE SET
                message_id = excluded.message_id,
                extra_message_ids = excluded.extra_message_ids,
                title = excluded.title,
                updated_at = excluded.updated_at
            """,
            (
                menu_key,
                int(message_id),
                ",".join(str(item) for item in extra_message_ids),
                title,
                int(time.time()),
            ),
        )

    def delete_menu_message(self, menu_key: str) -> None:
        self._conn.execute("DELETE FROM menu_messages WHERE menu_key = ?", (menu_key,))
