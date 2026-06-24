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
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InputMediaPhoto, Message

from . import messages
from .deps import Deps
from .formatting import display_title
from .telegram import safe_delete, safe_edit, safe_inline_edit
from ..infra.metrics import metrics
from ..models import PhotoAlbum, Track, VideoFile

# Telegram caption limit for a media-group item.
_CAPTION_LIMIT = 1024

logger = logging.getLogger(__name__)


async def _send_video(bot: Bot, chat_id: int, video: VideoFile):
    """Send a clip with ``send_video``, retrying without the thumbnail if Telegram
    rejects it. A fresh ``FSInputFile`` is built per attempt (the stream is
    one-shot). Returns the sent message."""
    kwargs = dict(
        caption=video.title or None,
        duration=video.duration,
        width=video.width,
        height=video.height,
        supports_streaming=True,
    )
    thumb = FSInputFile(video.thumb_path) if video.thumb_path else None
    try:
        return await bot.send_video(
            chat_id,
            FSInputFile(video.path, filename=video.filename),
            thumbnail=thumb,
            **kwargs,
        )
    except TelegramBadRequest:
        if thumb is None:
            raise
        logger.warning(
            "send_video rejected the thumbnail; retrying without it", exc_info=True
        )
        return await bot.send_video(
            chat_id,
            FSInputFile(video.path, filename=video.filename),
            **kwargs,
        )


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
    """Download the TikTok post at ``url`` and send it to ``chat_id`` — a clip via
    ``send_video`` or a photo slideshow as a media-group album — driving ``status``
    through queued → downloading → uploading."""
    bot = status.bot

    queued = deps.limiter.busy(user_id)
    if queued:
        await safe_edit(status, messages.QUEUED)

    async with deps.limiter.slot(user_id):
        if queued:
            await safe_edit(status, messages.DOWNLOADING_VIDEO)
        with tempfile.TemporaryDirectory() as workdir:
            try:
                media = await deps.video.download(url, workdir)
            except Exception:  # noqa: BLE001
                metrics.incr("downloads_failed")
                logger.exception("Video download failed")
                # A TikTok link can be a profile/channel or a photo post (no MP4)
                # — "no video" is clearer than a raw yt-dlp error string.
                await safe_edit(status, messages.NO_VIDEO)
                return

            if isinstance(media, PhotoAlbum):
                await _deliver_album(deps, chat_id, media, status)
                return

            if not media.exists:
                await safe_edit(status, messages.NO_VIDEO)
                return

            if media.size > deps.settings.max_file_size:
                await safe_edit(
                    status,
                    messages.too_large(media.size, deps.settings.max_file_size),
                )
                return

            await safe_edit(status, messages.UPLOADING)
            try:
                await bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
                await _send_video(bot, chat_id, media)
                metrics.incr("downloads_ok")
                await safe_delete(status)
            except Exception as exc:  # noqa: BLE001
                metrics.incr("sends_failed")
                logger.exception("Video send failed")
                await safe_edit(status, messages.send_failed(exc))


async def _deliver_album(
    deps: Deps, chat_id: int, album: PhotoAlbum, status: Message
) -> None:
    """Send a TikTok photo post's images as a Telegram album (or a single photo)."""
    bot = status.bot
    paths = album.existing
    if not paths:
        await safe_edit(status, messages.NO_VIDEO)
        return

    caption = album.title or None
    if caption and len(caption) > _CAPTION_LIMIT:
        caption = caption[: _CAPTION_LIMIT - 1] + "…"

    await safe_edit(status, messages.UPLOADING)
    try:
        await bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
        if len(paths) == 1:
            # A media group needs ≥2 items; a lone image goes via send_photo.
            await bot.send_photo(
                chat_id, FSInputFile(paths[0]), caption=caption
            )
        else:
            group = [
                InputMediaPhoto(
                    media=FSInputFile(path),
                    caption=caption if index == 0 else None,
                )
                for index, path in enumerate(paths)
            ]
            await bot.send_media_group(chat_id, media=group)
        metrics.incr("downloads_ok")
        await safe_delete(status)
    except Exception as exc:  # noqa: BLE001
        metrics.incr("sends_failed")
        logger.exception("Album send failed")
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
                # A photo slideshow can't be returned as a single inline file.
                if isinstance(video, PhotoAlbum) or not video.exists:
                    await safe_inline_edit(bot, inline_message_id, messages.NO_VIDEO)
                    return None
                if video.size > deps.settings.max_file_size:
                    await safe_inline_edit(
                        bot,
                        inline_message_id,
                        messages.too_large(video.size, deps.settings.max_file_size),
                    )
                    return None
                sent = await _send_video(bot, deps.settings.storage_chat_id, video)
    except Exception:  # noqa: BLE001
        logger.exception("Inline video download failed")
        # A TikTok link can fail because it's a profile/channel or an otherwise
        # un-downloadable post — "no video" is more accurate (and less alarming)
        # than a generic search error.
        await safe_inline_edit(bot, inline_message_id, messages.NO_VIDEO)
        return None

    return sent.video.file_id if sent.video else None
