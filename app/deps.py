"""The dependency bundle shared across all handlers.

Built once in ``application.py`` and closed over by ``build_router``; passing a
single object keeps handler signatures small and the wiring explicit. Living in
its own module lets both ``handlers`` and ``delivery`` depend on it without an
import cycle.
"""
from __future__ import annotations

from dataclasses import dataclass

from .caches import SearchCache, TrackCache
from .config import Settings
from .limiter import DownloadLimiter, RateLimiter
from .service import MusicService
from .store import FileIdStore


@dataclass
class Deps:
    settings: Settings
    service: MusicService
    limiter: DownloadLimiter
    rate: RateLimiter
    cache: SearchCache
    files: FileIdStore
    inline: TrackCache
    # Resolved from the running bot at startup (bot.get_me); used for the inline
    # attribution links. Set by application.py before any update is processed.
    bot_username: str = ""
