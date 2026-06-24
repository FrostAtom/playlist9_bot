"""Composition root: wire settings, sources, service, handlers, run the bot."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from .bot.caches import InlineCache, LinkCache, SearchCache
from .bot.deps import Deps
from .bot.router import build_router
from .config import Settings
from .infra import store
from .infra.health import heartbeat
from .infra.limiter import DownloadLimiter, RateLimiter
from .infra.metrics import MetricsLogHandler, metrics
from .infra.store import FileIdStore
from .music.service import MusicService
from .music.sources.soundcloud import SoundCloudSource
from .music.sources.youtube import YouTubeMusicSource
from .music.video import VideoDownloader
from .web.server import start_download_server, start_web_server

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
    # Capture WARNING+ logs into the in-memory buffer the status page reads.
    log_handler = MetricsLogHandler(metrics)
    log_handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(log_handler)

    bot = Bot(token=settings.token)
    dispatcher = Dispatcher()
    # The file_id store is mandatory; create_pool raises SystemExit (→ container
    # restart) if the database can't be reached, so we never run without it.
    pool = await store.create_pool(
        host=settings.database_host,
        port=settings.database_port,
        user=settings.database_user,
        password=settings.database_password,
        database=settings.database_name,
    )
    deps = Deps(
        settings=settings,
        service=build_service(settings),
        video=VideoDownloader(cookiefile=settings.cookies_file or None),
        limiter=DownloadLimiter(
            per_user=settings.download_per_user, total=settings.download_total
        ),
        rate=RateLimiter(settings.rate_per_minute, 60.0),
        cache=SearchCache(settings.search_cache_size),
        files=FileIdStore(pool),
        inline=InlineCache(),
        links=LinkCache(settings.search_cache_size),
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
        if settings.metrics_port:
            try:
                state["web"] = await start_web_server(
                    metrics, settings.metrics_host, settings.metrics_port
                )
            except Exception:  # noqa: BLE001 - the status page must never block startup
                logger.warning("Status page failed to start", exc_info=True)
        if settings.web_port:
            try:
                state["download_web"] = await start_download_server(
                    deps.service,
                    settings,
                    deps.limiter,
                    deps.rate,
                    settings.web_host,
                    settings.web_port,
                )
            except Exception:  # noqa: BLE001 - the download page must never block startup
                logger.warning("Download page failed to start", exc_info=True)
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
        for key in ("web", "download_web"):
            runner = state.get(key)
            if runner is not None:
                await runner.cleanup()
        if pool is not None:
            await pool.close()
        await bot.session.close()
        logger.info("Bye")

    await dispatcher.start_polling(bot)


def run() -> None:
    asyncio.run(_amain(Settings.from_env()))
