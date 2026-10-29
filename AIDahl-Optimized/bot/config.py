"""
Central configuration – all environment variables live here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Set

from dotenv import load_dotenv

load_dotenv()


def _bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _int(val: str | None, default: int) -> int:
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _float(val: str | None, default: float) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # Core
    BOT_TOKEN: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", ""))
    SUPABASE_URL: str = field(default_factory=lambda: os.getenv("SUPABASE_URL", "").rstrip("/"))
    SUPABASE_KEY: str = field(default_factory=lambda: os.getenv("SUPABASE_KEY", ""))
    BOT_VERSION: str = field(default_factory=lambda: os.getenv("BOT_VERSION", "2.0.0-optimized"))

    # Dahl / shared
    DAHL_API_KEY: str = field(default_factory=lambda: os.getenv("DAHL_API_KEY", ""))
    DAHL_BASE_URL: str = field(
        default_factory=lambda: os.getenv("DAHL_BASE_URL", "https://inference.dahl.global/v1").rstrip("/")
    )
    ALLOW_SHARED_KEY: bool = field(default_factory=lambda: _bool(os.getenv("ALLOW_SHARED_KEY"), True))

    # Quotas
    TOKEN_QUOTA: int = field(default_factory=lambda: _int(os.getenv("TOKEN_QUOTA"), 1_000_000))
    DEFAULT_DAILY_TOKEN_QUOTA: int = field(
        default_factory=lambda: _int(os.getenv("DEFAULT_DAILY_TOKEN_QUOTA"), 50_000)
    )
    DEFAULT_LIFETIME_TOKEN_QUOTA: int = field(
        default_factory=lambda: _int(os.getenv("DEFAULT_LIFETIME_TOKEN_QUOTA"), 3_000_000)
    )
    LIFETIME_WARN_PCT: float = field(default_factory=lambda: _float(os.getenv("LIFETIME_WARN_PCT"), 75.0))
    QUOTA_APPLIES_TO_OWN_KEYS: bool = field(
        default_factory=lambda: _bool(os.getenv("QUOTA_APPLIES_TO_OWN_KEYS"), False)
    )

    # History
    HISTORY_LIMIT: int = field(default_factory=lambda: _int(os.getenv("HISTORY_LIMIT"), 20))
    KEEP_MESSAGES_IN_ACTIVE_CHAT: int = field(
        default_factory=lambda: _int(os.getenv("KEEP_MESSAGES_IN_ACTIVE_CHAT"), 40)
    )
    SKIP_USAGE_TABLE: bool = field(default_factory=lambda: _bool(os.getenv("SKIP_USAGE_TABLE"), True))

    # Streaming
    STREAMING_ENABLED: bool = field(default_factory=lambda: _bool(os.getenv("STREAMING_ENABLED"), True))
    STREAM_EDIT_INTERVAL: float = field(
        default_factory=lambda: _float(os.getenv("STREAM_EDIT_INTERVAL"), 1.2)
    )

    # Admin
    ADMIN_TELEGRAM_IDS: Set[int] = field(default_factory=set)

    # Manus
    MANUS_API_KEY: str = field(
        default_factory=lambda: os.getenv("MANUS_API_KEY") or os.getenv("MANUS_SHARED_API_KEY") or ""
    )
    MANUS_BASE_URL: str = field(
        default_factory=lambda: os.getenv("MANUS_BASE_URL", "https://api.manus.ai").rstrip("/")
    )
    MANUS_DEFAULT_PROFILE: str = field(
        default_factory=lambda: os.getenv("MANUS_DEFAULT_PROFILE", "standard")
    )
    MANUS_TIMEOUT_SEC: int = field(default_factory=lambda: _int(os.getenv("MANUS_TIMEOUT_SEC"), 90))
    MANUS_POLL_INTERVAL: float = field(
        default_factory=lambda: _float(os.getenv("MANUS_POLL_INTERVAL"), 4.0)
    )
    MANUS_LOCALE: str = field(default_factory=lambda: os.getenv("MANUS_LOCALE", "").strip())
    MANUS_MAX_CONCURRENT: int = field(default_factory=lambda: max(1, _int(os.getenv("MANUS_MAX_CONCURRENT"), 2)))
    MANUS_QUEUE_MAX: int = field(default_factory=lambda: max(1, _int(os.getenv("MANUS_QUEUE_MAX"), 15)))
    MANUS_MAX_PER_USER: int = field(default_factory=lambda: max(1, _int(os.getenv("MANUS_MAX_PER_USER"), 1)))
    MANUS_FOLLOWUP_TTL_SEC: int = field(
        default_factory=lambda: _int(os.getenv("MANUS_FOLLOWUP_TTL_SEC"), 7200)
    )
    MANUS_STALE_LOCK_SEC: int = field(
        default_factory=lambda: _int(os.getenv("MANUS_STALE_LOCK_SEC"), 180)
    )

    # Security
    ENCRYPTION_KEY: str = field(default_factory=lambda: os.getenv("ENCRYPTION_KEY", ""))

    # UI
    PROMPTOPIA_URL: str = field(
        default_factory=lambda: os.getenv(
            "PROMPTOPIA_URL", "https://mohsen-niksirat.github.io/promptopia/"
        ).strip()
    )
    PROMPTOPIA_LABEL: str = field(
        default_factory=lambda: os.getenv("PROMPTOPIA_LABEL", "📚 پرامپت‌های آماده")
    )

    # Constants
    MAX_TG_MESSAGE: int = 4000
    CHAT_LIST_LIMIT: int = 15
    SYSTEM_PROMPT: str = (
        "You are a helpful AI assistant on Telegram. "
        "Answer clearly. Use short paragraphs, lists, and code blocks when useful."
    )

    def __post_init__(self):
        # Parse admin IDs
        raw = os.getenv("ADMIN_TELEGRAM_IDS") or ""
        ids = set()
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))
        object.__setattr__(self, "ADMIN_TELEGRAM_IDS", ids)


# Singleton
settings = Settings()


def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_TELEGRAM_IDS