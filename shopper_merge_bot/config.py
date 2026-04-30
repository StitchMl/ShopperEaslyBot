from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    """Raised when the service is missing required configuration."""


def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return

    load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc


def _list(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _int_list(name: str) -> tuple[int, ...]:
    values = []
    for item in _list(name):
        try:
            values.append(int(item))
        except ValueError as exc:
            raise ConfigError(f"{name} must contain only integer IDs") from exc
    return tuple(values)


def parse_chat_ref(value: str) -> str | int:
    cleaned = value.strip()
    if cleaned.lstrip("-").isdigit():
        return int(cleaned)
    return cleaned


@dataclass(frozen=True)
class Settings:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session: str
    source_chats: tuple[str | int, ...]
    destination_chat: str | int | None
    telegram_bot_token: str | None
    admin_user_ids: tuple[int, ...]
    database_path: Path
    monitor_all_chats: bool
    join_sources: bool
    copy_media: bool
    include_source_link: bool
    dry_run: bool
    startup_backfill_limit: int
    dedupe_ttl_days: int
    min_post_interval_seconds: float
    max_text_chars: int
    allow_patterns: tuple[str, ...]
    skip_patterns: tuple[str, ...]
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv_if_available()

        try:
            api_id = int(_required("TELEGRAM_API_ID"))
        except ValueError as exc:
            raise ConfigError("TELEGRAM_API_ID must be an integer") from exc

        monitor_all = _bool("MONITOR_ALL_CHATS", False)
        source_chats = tuple(parse_chat_ref(item) for item in _list("SOURCE_CHATS"))

        destination_value = _optional("DESTINATION_CHAT")
        destination = parse_chat_ref(destination_value) if destination_value else None
        bot_token = _optional("TELEGRAM_BOT_TOKEN") or None
        if not source_chats and not monitor_all and not bot_token:
            raise ConfigError(
                "Set SOURCE_CHATS, set MONITOR_ALL_CHATS=true, or configure TELEGRAM_BOT_TOKEN "
                "and add sources with /folder."
            )

        return cls(
            telegram_api_id=api_id,
            telegram_api_hash=_required("TELEGRAM_API_HASH"),
            telegram_session=_required("TELEGRAM_SESSION"),
            source_chats=source_chats,
            destination_chat=destination,
            telegram_bot_token=bot_token,
            admin_user_ids=_int_list("ADMIN_USER_IDS"),
            database_path=Path(_optional("DATABASE_PATH", "data/shopperbot.sqlite3")),
            monitor_all_chats=monitor_all,
            join_sources=_bool("JOIN_SOURCES", False),
            copy_media=_bool("COPY_MEDIA", True),
            include_source_link=_bool("INCLUDE_SOURCE_LINK", True),
            dry_run=_bool("DRY_RUN", False),
            startup_backfill_limit=_int("STARTUP_BACKFILL_LIMIT", 0),
            dedupe_ttl_days=_int("DEDUPE_TTL_DAYS", 14),
            min_post_interval_seconds=_float("MIN_POST_INTERVAL_SECONDS", 1.0),
            max_text_chars=_int("MAX_TEXT_CHARS", 3500),
            allow_patterns=_list("ALLOW_PATTERNS"),
            skip_patterns=_list("SKIP_PATTERNS"),
            log_level=_optional("LOG_LEVEL", "INFO").upper(),
        )

    def as_log_safe_dict(self) -> dict[str, Any]:
        return {
            "source_chats": self.source_chats,
            "destination_chat": self.destination_chat,
            "database_path": str(self.database_path),
            "monitor_all_chats": self.monitor_all_chats,
            "join_sources": self.join_sources,
            "copy_media": self.copy_media,
            "include_source_link": self.include_source_link,
            "dry_run": self.dry_run,
            "startup_backfill_limit": self.startup_backfill_limit,
            "dedupe_ttl_days": self.dedupe_ttl_days,
            "min_post_interval_seconds": self.min_post_interval_seconds,
            "max_text_chars": self.max_text_chars,
            "allow_patterns": self.allow_patterns,
            "skip_patterns": self.skip_patterns,
            "log_level": self.log_level,
            "uses_bot_sender": self.telegram_bot_token is not None,
            "admin_user_ids_count": len(self.admin_user_ids),
        }
