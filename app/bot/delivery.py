"""Download → validate → send pipeline, shared by the chat and inline flows.

Kept separate from the router so the orchestration (concurrency slot, file-size
check, file_id caching) reads independently of the aiogram update handling.
"""
from __future__ import annotations

import logging
import tempfile
from typing import Optional, Union

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.types import FSInputFile, Message

from . import messages
from .deps import Deps
from .formatting import display_title
from .telegram import safe_delete, safe_edit, safe_inline_edit
from ..infra.metrics import metrics
from ..models import Track

logger = logging.getLogger(__name__)


async def deliver(
    deps: Deps,
    chat_id: int,
    user_id: int,
    ref: Union[Track, str],
    status: Message,
) -> None:
    """Download ``ref`` (a picked ``Track`` or a raw URL) and send it to
    ``chat_id``, driving ``status`` through queued → downloading → uploading."""
    bot = status.bot

    queued = deps.limiter.busy(user_id)
    if queued:
        await safe_edit(status, messages.QUEUED)

    async with deps.limiter.slot(user_id):
        if queued:
            await safe_edit(status, messages.DOWNLOADING)
        with tempfile.TemporaryDirectory() as workdir:
            try:
                audio = await deps.service.download(ref, workdir)
            except Exception as exc:  # noqa: BLE001
                metrics.incr("downloads_failed")
                logger.exception("Download failed")
                await safe_edit(status, messages.download_failed(exc))
                return

            if not audio.exists:
                await safe_edit(status, messages.NO_AUDIO)
                return

            if audio.size > deps.settings.max_file_size:
                await safe_edit(
                    status,
                    messages.too_large(audio.size, deps.settings.max_file_size),
                )
                return

            await safe_edit(status, messages.UPLOADING)
            try:
                await bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
                thumbnail = (
                    FSInputFile(audio.thumb_path) if audio.thumb_path else None
                )
                sent = await bot.send_audio(
                    chat_id,
                    FSInputFile(audio.path, filename=audio.filename),
                    title=audio.title,
                    performer=audio.uploader,
                    duration=audio.duration,
                    thumbnail=thumbnail,
                )
                if isinstance(ref, Track) and sent.audio:
                    await deps.files.put(
                        deps.files.key(ref.source, ref.id), sent.audio.file_id
                    )
                metrics.incr("downloads_ok")
                await safe_delete(status)
            except Exception as exc:  # noqa: BLE001
                metrics.incr("sends_failed")
                logger.exception("Send failed")
                await safe_edit(status, messages.send_failed(exc))


async def deliver_video(
    deps: Deps,
    chat_id: int,
    user_id: int,
    url: str,
    status: Message,
) -> None:
    """Download the TikTok clip at ``url`` and send it as a video to ``chat_id``,
    driving ``status`` through queued → downloading → uploading."""
    bot = status.bot

    queued = deps.limiter.busy(user_id)
    if queued:
        await safe_edit(status, messages.QUEUED)

    async with deps.limiter.slot(user_id):
        if queued:
            await safe_edit(status, messages.DOWNLOADING_VIDEO)
        with tempfile.TemporaryDirectory() as workdir:
            try:
                video = await deps.video.download(url, workdir)
            except Exception as exc:  # noqa: BLE001
                metrics.incr("downloads_failed")
                logger.exception("Video download failed")
                await safe_edit(status, messages.download_failed(exc))
                return

            if not video.exists:
                await safe_edit(status, messages.NO_VIDEO)
                return

            if video.size > deps.settings.max_file_size:
                await safe_edit(
                    status,
                    messages.too_large(video.size, deps.settings.max_file_size),
                )
                return

            await safe_edit(status, messages.UPLOADING)
            try:
                await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
                thumbnail = (
                    FSInputFile(video.thumb_path) if video.thumb_path else None
                )
                await bot.send_video(
                    chat_id,
                    FSInputFile(video.path, filename=video.filename),
                    caption=video.title or None,
                    duration=video.duration,
                    width=video.width,
                    height=video.height,
                    thumbnail=thumbnail,
                    supports_streaming=True,
                )
                metrics.incr("downloads_ok")
                await safe_delete(status)
            except Exception as exc:  # noqa: BLE001
                metrics.incr("sends_failed")
                logger.exception("Video send failed")
                await safe_edit(status, messages.send_failed(exc))


async def ensure_file_id(
    deps: Deps, bot: Bot, track: Track, inline_message_id: str, user_id: int
) -> Optional[str]:
    """Return a Telegram file_id for ``track``, downloading + uploading it to the
    storage chat if needed. Edits the inline message with an error otherwise."""
    # Query-only items (Spotify/Apple playlist picks) have no stable id/url, so
    # their cache key would collide across playlists — skip the persistent cache.
    cacheable = bool(track.url)
    key = deps.files.key(track.source, track.id)
    if cacheable:
        file_id = await deps.files.get(key)
        if file_id:
            return file_id

    if not deps.settings.storage_chat_id:
        title = display_title(track)
        await safe_inline_edit(bot, inline_message_id, f"🎵 {title}\n{track.url}")
        return None

    if not deps.rate.allow(user_id):
        metrics.incr("rate_limited")
        await safe_inline_edit(
            bot,
            inline_message_id,
            messages.rate_limited(
                deps.settings.rate_per_minute, deps.rate.retry_after(user_id)
            ),
        )
        return None

    try:
        async with deps.limiter.slot(user_id):
            with tempfile.TemporaryDirectory() as workdir:
                audio = await deps.service.download(track, workdir)
                if not audio.exists:
                    await safe_inline_edit(bot, inline_message_id, messages.NO_AUDIO)
                    return None
                if audio.size > deps.settings.max_file_size:
                    await safe_inline_edit(
                        bot,
                        inline_message_id,
                        messages.too_large(audio.size, deps.settings.max_file_size),
                    )
                    return None
                sent = await bot.send_audio(
                    deps.settings.storage_chat_id,
                    FSInputFile(audio.path, filename=audio.filename),
                    title=audio.title,
                    performer=audio.uploader,
                    duration=audio.duration,
                    thumbnail=(
                        FSInputFile(audio.thumb_path) if audio.thumb_path else None
                    ),
                )
    except Exception:  # noqa: BLE001
        logger.exception("Inline download failed")
        await safe_inline_edit(bot, inline_message_id, messages.SEARCH_ERROR)
        return None

    if not sent.audio:
        return None
    if cacheable:
        await deps.files.put(key, sent.audio.file_id)
    return sent.audio.file_id


async def ensure_video_file_id(
    deps: Deps, bot: Bot, url: str, inline_message_id: str, user_id: int
) -> Optional[str]:
    """Download the TikTok clip at ``url``, upload it to the storage chat to mint a
    Telegram video file_id, and return it. Edits the inline message on failure.

    Unlike audio, video file_ids aren't persisted — TikTok links are one-off, so
    the storage round-trip happens fresh each time."""
    if not deps.settings.storage_chat_id:
        await safe_inline_edit(bot, inline_message_id, f"🎬 {url}")
        return None

    if not deps.rate.allow(user_id):
        metrics.incr("rate_limited")
        await safe_inline_edit(
            bot,
            inline_message_id,
            messages.rate_limited(
                deps.settings.rate_per_minute, deps.rate.retry_after(user_id)
            ),
        )
        return None

    try:
        async with deps.limiter.slot(user_id):
            with tempfile.TemporaryDirectory() as workdir:
                video = await deps.video.download(url, workdir)
                if not video.exists:
                    await safe_inline_edit(bot, inline_message_id, messages.NO_VIDEO)
                    return None
                if video.size > deps.settings.max_file_size:
                    await safe_inline_edit(
                        bot,
                        inline_message_id,
                        messages.too_large(video.size, deps.settings.max_file_size),
                    )
                    return None
                sent = await bot.send_video(
                    deps.settings.storage_chat_id,
                    FSInputFile(video.path, filename=video.filename),
                    duration=video.duration,
                    width=video.width,
                    height=video.height,
                    thumbnail=(
                        FSInputFile(video.thumb_path) if video.thumb_path else None
                    ),
                    supports_streaming=True,
                )
    except Exception:  # noqa: BLE001
        logger.exception("Inline video download failed")
        await safe_inline_edit(bot, inline_message_id, messages.SEARCH_ERROR)
        return None

    return sent.video.file_id if sent.video else None
