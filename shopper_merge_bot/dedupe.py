from __future__ import annotations

import sqlite3
from pathlib import Path

from .storage.admins import AdminStoreMixin
from .storage.config import ConfigStoreMixin
from .storage.menus import MenuStoreMixin
from .storage.models import MenuMessage, OfferRecord, OfferSource, SourceChat
from .storage.offers import OfferStoreMixin
from .storage.schema import SchemaStoreMixin
from .storage.seen import SeenStoreMixin
from .storage.sources import SourceStoreMixin


class DedupeStore(
    SchemaStoreMixin,
    ConfigStoreMixin,
    SourceStoreMixin,
    AdminStoreMixin,
    OfferStoreMixin,
    MenuStoreMixin,
    SeenStoreMixin,
):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def close(self) -> None:
        self._conn.close()


__all__ = [
    "DedupeStore",
    "MenuMessage",
    "OfferRecord",
    "OfferSource",
    "SourceChat",
]
