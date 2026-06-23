"""Application configuration loaded from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


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
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN environment variable is not set")
        storage = os.environ.get("STORAGE_CHAT_ID")
        return cls(
            token=token,
            max_file_size=int(os.environ.get("MAX_FILE_SIZE_MB", "50")) * 1024 * 1024,
            max_results=int(os.environ.get("MAX_RESULTS", "30")),
            results_per_page=int(os.environ.get("RESULTS_PER_PAGE", "10")),
            audio_quality=os.environ.get("AUDIO_QUALITY", "192"),
            storage_chat_id=int(storage) if storage else None,
        )
