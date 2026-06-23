"""Application configuration loaded from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _env_int(name: str, default: int) -> int:
    """Read an int env var, treating missing/empty/blank as the default.

    Empty strings are common when an env var is declared but unset (e.g. compose
    `${VAR}` interpolation), and would otherwise crash `int('')`.
    """
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


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
    audio_quality: str = "192"
    # Chat (e.g. a private channel where the bot is admin) used to upload freshly
    # downloaded tracks and obtain a file_id, so inline mode can deliver files.
    # When unset, inline mode can only re-send already-cached tracks.
    storage_chat_id: Optional[int] = None

    @classmethod
    def from_env(cls) -> "Settings":
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN environment variable is not set")
        return cls(
            token=token,
            max_file_size=_env_int("MAX_FILE_SIZE_MB", 50) * 1024 * 1024,
            max_results=_env_int("MAX_RESULTS", 30),
            results_per_page=_env_int("RESULTS_PER_PAGE", 10),
            audio_quality=_env_str("AUDIO_QUALITY", "192"),
            storage_chat_id=_env_int("STORAGE_CHAT_ID", 0) or None,
        )
