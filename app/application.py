"""Composition root: wire settings, sources, service, handlers, run the bot."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from . import store
from .caches import SearchCache, TrackCache
from .config import Settings
from .deps import Deps
from .handlers import build_router
from .health import heartbeat
from .limiter import DownloadLimiter, RateLimiter
from .service import MusicService
from .sources.soundcloud import SoundCloudSource
from .sources.youtube import YouTubeMusicSource
from .store import FileIdStore

logger = logging.getLogger(__name__)


def build_service(settings: Settings) -> MusicService:
    # Register additional AudioSource implementations here to extend coverage.
    # The first source is the default for URLs no source explicitly claims.
    quality = settings.audio_quality
    cookies = settings.cookies_file or None
    return MusicService(
        [
            YouTubeMusicSource(audio_quality=quality, cookiefile=cookies),
            SoundCloudSource(audio_quality=quality, cookiefile=cookies),
        ]
    )


async def _amain(settings: Settings) -> None:
    bot = Bot(token=settings.token)
    dispatcher = Dispatcher()
    # The file_id store is mandatory; create_pool raises SystemExit (→ container
    # restart) if the database can't be reached, so we never run without it.
    pool = await store.create_pool(
        settings.database_url, password=settings.database_password
    )
    deps = Deps(
        settings=settings,
        service=build_service(settings),
        limiter=DownloadLimiter(
            per_user=settings.download_per_user, total=settings.download_total
        ),
        rate=RateLimiter(settings.rate_per_minute, 60.0),
        cache=SearchCache(settings.search_cache_size),
        files=FileIdStore(pool),
        inline=TrackCache(),
    )
    dispatcher.include_router(build_router(deps))

    # aiogram installs SIGINT/SIGTERM handlers itself and stops polling
    # gracefully; we hook startup/shutdown for the heartbeat and cleanup.
    state: dict = {}

    @dispatcher.startup()
    async def _on_startup() -> None:
        # Resolve the bot's own @username so inline attribution links are always
        # correct, regardless of how the bot is named/renamed.
        me = await bot.get_me()
        deps.bot_username = me.username or ""
        state["heartbeat"] = asyncio.create_task(heartbeat())
        if settings.storage_chat_id:
            try:
                chat = await bot.get_chat(settings.storage_chat_id)
                logger.info("Storage chat OK: %s (%s)", chat.title, chat.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "STORAGE_CHAT_ID=%s is not reachable (%s). Inline file "
                    "delivery for new tracks will fail — add the bot to that "
                    "chat as an admin.",
                    settings.storage_chat_id,
                    exc,
                )
        logger.info("Bot started as @%s", deps.bot_username)

    @dispatcher.shutdown()
    async def _on_shutdown() -> None:
        logger.info("Shutting down...")
        task = state.get("heartbeat")
        if task:
            task.cancel()
        if pool is not None:
            await pool.close()
        await bot.session.close()
        logger.info("Bye")

    await dispatcher.start_polling(bot)


def run() -> None:
    asyncio.run(_amain(Settings.from_env()))
