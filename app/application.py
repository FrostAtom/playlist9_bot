"""Composition root: wire settings, sources, service, handlers, run the bot."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from .config import Settings
from .handlers import (
    Deps,
    FileIdCache,
    History,
    SearchCache,
    TrackCache,
    build_router,
)
from .health import heartbeat
from .limiter import DownloadLimiter
from .service import MusicService
from .sources.soundcloud import SoundCloudSource
from .sources.youtube import YouTubeMusicSource

logger = logging.getLogger(__name__)


def build_service(settings: Settings) -> MusicService:
    # Register additional AudioSource implementations here to extend coverage.
    # The first source is the default for URLs no source explicitly claims.
    quality = settings.audio_quality
    return MusicService(
        [
            YouTubeMusicSource(audio_quality=quality),
            SoundCloudSource(audio_quality=quality),
        ]
    )


async def _amain(settings: Settings) -> None:
    bot = Bot(token=settings.token)
    dispatcher = Dispatcher()
    deps = Deps(
        settings=settings,
        service=build_service(settings),
        limiter=DownloadLimiter(per_user=3, total=8),
        cache=SearchCache(),
        history=History(),
        files=FileIdCache(),
        inline=TrackCache(),
    )
    dispatcher.include_router(build_router(deps))

    # aiogram installs SIGINT/SIGTERM handlers itself and stops polling
    # gracefully; we hook startup/shutdown for the heartbeat and cleanup.
    state: dict = {}

    @dispatcher.startup()
    async def _on_startup() -> None:
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
        logger.info("Bot started")

    @dispatcher.shutdown()
    async def _on_shutdown() -> None:
        logger.info("Shutting down...")
        task = state.get("heartbeat")
        if task:
            task.cancel()
        await bot.session.close()
        logger.info("Bye")

    await dispatcher.start_polling(bot)


def run() -> None:
    asyncio.run(_amain(Settings.from_env()))
