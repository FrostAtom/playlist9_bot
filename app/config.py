"""Application configuration loaded from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _env_int(name: str, default: int) -> int:
    """Read an int env var, treating missing/empty/blank as the default.

    Empty strings are common when an env var is declared but unset (e.g. compose
    `${VAR}` interpolation), and would otherwise crash `int('')`. A non-numeric
    value is a config mistake, so fail fast with a clear message.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}")


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name, "").strip()
    return raw or default


@dataclass(frozen=True)
class Settings:
    token: str
    # Telegram Bot API caps uploads at 50 MB for regular bots.
    max_file_size: int = 50 * 1024 * 1024
    # Total number of results to fetch for a query.
    max_results: int = 30
    # How many results to show per page (one track per button).
    results_per_page: int = 10
    # MP3 quality in kbps; the best available source audio is fetched and
    # transcoded at this bitrate (320 = highest MP3 quality).
    audio_quality: str = "320"
    # Results fetched for an inline query.
    inline_results: int = 20
    # How many recent searches to remember per user (for pagination/selection).
    search_cache_size: int = 20
    # Seconds a search-results message lives before the bot auto-deletes it.
    results_ttl: int = 300
    # Concurrent download limits: per single user and across all users.
    download_per_user: int = 3
    download_total: int = 8
    # Max downloads a single user may trigger per minute (abuse throttle).
    rate_per_minute: int = 10
    # Chat (e.g. a private channel where the bot is admin) used to upload freshly
    # downloaded tracks and obtain a file_id, so inline mode can deliver files.
    # When unset, inline mode can only re-send already-cached tracks.
    storage_chat_id: Optional[int] = None
    # PostgreSQL DSN for the persistent file_id cache. Required in production
    # (the bot refuses to start without a reachable database). Left blank only
    # for offline unit tests that build a MusicService directly.
    database_url: str = ""
    # Database password, supplied separately from the DSN so special characters
    # can't corrupt the URL; asyncpg applies it verbatim. Empty means the
    # password (if any) is taken from the DSN as-is.
    database_password: str = ""
    # Path to a Netscape-format cookies.txt passed to yt-dlp (for age-restricted
    # or region-locked content). When unset or missing, no cookies are used.
    cookies_file: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN environment variable is not set")
        database_url = _env_str("DATABASE_URL", "")
        if not database_url:
            raise SystemExit("DATABASE_URL environment variable is not set")
        return cls(
            token=token,
            max_file_size=_env_int("MAX_FILE_SIZE_MB", 50) * 1024 * 1024,
            max_results=_env_int("MAX_RESULTS", 30),
            results_per_page=_env_int("RESULTS_PER_PAGE", 10),
            audio_quality=_env_str("AUDIO_QUALITY", "320"),
            inline_results=_env_int("INLINE_RESULTS", 20),
            search_cache_size=_env_int("SEARCH_CACHE_SIZE", 20),
            results_ttl=_env_int("RESULTS_TTL_SECONDS", 300),
            download_per_user=_env_int("DOWNLOAD_PER_USER", 3),
            download_total=_env_int("DOWNLOAD_TOTAL", 8),
            rate_per_minute=_env_int("RATE_PER_MINUTE", 10),
            storage_chat_id=_env_int("STORAGE_CHAT_ID", 0) or None,
            database_url=database_url,
            database_password=_env_str("DATABASE_PASSWORD", ""),
            cookies_file=_env_str("COOKIES_FILE", ""),
        )
