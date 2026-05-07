from __future__ import annotations

from decimal import Decimal


class ConfigStoreMixin:


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
