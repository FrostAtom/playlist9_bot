"""Inline-mode orchestration.

The inline query can be a plain search *or* a pasted link — a YouTube/SoundCloud
track or playlist, a Spotify/Apple track or playlist, or a TikTok clip. This
module classifies the query (mirroring ``router.on_text``) and turns it into
inline results, then finishes a chosen result by minting a Telegram ``file_id``.

Kept out of the (deliberately thin) router so the link classification and the
two-step file_id dance read on their own. Telegram constraints worth recalling:
a file can only be sent inline via a ``file_id``, which can't be produced during
the inline query — so uncached items are offered as article placeholders that
``handle_chosen`` swaps for real media once the user picks one.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import List

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InputMediaAudio,
    InputMediaVideo,
    InputTextMessageContent,
)

from . import delivery, formatting, messages
from .caches import InlineRef
from .deps import Deps
from .telegram import safe_inline_edit
from ..infra.metrics import metrics
from ..models import Track
from ..music import links
from ..music.video import detect_tiktok

logger = logging.getLogger(__name__)


async def answer(deps: Deps, query: InlineQuery) -> None:
    """Resolve an inline query (text or link) and answer with inline results."""
    # Inline mode is meant for *other* chats. In the bot's own chat the user can
    # just send a message, so suppress inline results there (chat_type "sender").
    if query.chat_type == "sender":
        await query.answer([], cache_time=300, is_personal=True)
        return

    text = query.query.strip()
    if not text:
        await query.answer([], cache_time=5, is_personal=True)
        return

    metrics.incr("inline_queries")
    limit = deps.settings.inline_results

    # A TikTok link is a video, not a track — offer the clip as its own result.
    tiktok_url = detect_tiktok(text)
    if tiktok_url:
        await query.answer(
            [_tiktok_result(deps, tiktok_url)], cache_time=0, is_personal=True
        )
        return

    try:
        tracks = await _resolve_tracks(deps, text, limit)
    except Exception:  # noqa: BLE001
        logger.exception("Inline resolve failed")
        await query.answer([], cache_time=5)
        return

    if not tracks:
        await query.answer([], cache_time=5, is_personal=True)
        return

    results = await _track_results(deps, text, tracks[:limit])
    # cache_time=0: results depend on per-track cache state, don't cache.
    await query.answer(results, cache_time=0, is_personal=True)


async def handle_chosen(deps: Deps, bot: Bot, chosen: ChosenInlineResult) -> None:
    """Finish a chosen placeholder: download → upload → swap in the real media."""
    inline_message_id = chosen.inline_message_id
    if not inline_message_id:
        return  # cached-audio results need no follow-up
    ref = deps.inline.get(chosen.result_id)
    if ref is None:
        await safe_inline_edit(bot, inline_message_id, messages.RESULTS_EXPIRED)
        return
    if ref.tiktok_url:
        await _finish_video(
            deps, bot, ref.tiktok_url, inline_message_id, chosen.from_user.id
        )
    elif ref.track is not None:
        await _finish_audio(
            deps, bot, ref.track, inline_message_id, chosen.from_user.id
        )


# --- query classification -------------------------------------------------


async def _resolve_tracks(deps: Deps, text: str, limit: int) -> List[Track]:
    """Turn an inline query into a list of downloadable tracks (mirrors on_text).

    Everything except TikTok collapses to "a list of Tracks": a search, a single
    pasted track link, or the contents of a pasted playlist/album link."""
    match = deps.service.resolve(text)
    if match:
        info = deps.service.link_info(text)
        # A pure playlist link (SoundCloud set, YouTube /playlist) → its tracks.
        if info and info.playlist_url and not info.track_url:
            tracks, _ = await deps.service.playlist(info.playlist_url, limit, info.source)
            return tracks
        # A single track (an ambiguous track-in-playlist link prefers the track,
        # since inline mode can't ask which the user meant).
        url = info.track_url if info and info.track_url else match[1]
        track = await deps.service.resolve_track(url)
        return [track] if track else []

    # A Spotify/Apple playlist or album: scrape it into searchable items.
    if links.detect_playlist(text):
        playlist = await asyncio.to_thread(links.resolve_playlist, text, limit)
        if playlist is None or not playlist.items:
            return []
        source = deps.service.default_source()
        return [
            Track(
                id=f"q{i}",
                title=item.title,
                url="",
                uploader=item.artist or None,
                source=source,
                query=item.query,
            )
            for i, item in enumerate(playlist.items)
        ]

    # A Spotify/Apple track: resolve to a query and search YouTube Music.
    if links.detect(text):
        external = await asyncio.to_thread(links.resolve, text)
        if external is None:
            return []
        source = deps.service.default_source()
        return await deps.service.search(external.query, limit, source)

    # Plain text search.
    source = deps.service.default_source()
    return await deps.service.search(text, limit, source)


# --- result building ------------------------------------------------------


async def _track_results(deps: Deps, text: str, tracks: List[Track]) -> list:
    # One round-trip to resolve which (cacheable) results are already on Telegram.
    cacheable = [t for t in tracks if t.url]
    keys = [deps.files.key(t.source, t.id) for t in cacheable]
    cached = await deps.files.get_many(keys) if keys else {}

    results: list = []
    for index, track in enumerate(tracks):
        rid = _result_id(deps, text, track, index)
        title = formatting.display_title(track)
        file_id = (
            cached.get(deps.files.key(track.source, track.id)) if track.url else None
        )
        if file_id:
            # Already on Telegram's servers — send it as playable audio.
            results.append(InlineQueryResultCachedAudio(id=rid, audio_file_id=file_id))
            continue
        # Offer a placeholder; the file is downloaded once the user picks it
        # (handled in handle_chosen). A keyboard is required so Telegram returns
        # an inline_message_id we can edit into audio.
        deps.inline.put(rid, InlineRef(track=track))
        results.append(
            InlineQueryResultArticle(
                id=rid,
                title=title,
                description=track.album
                or formatting.SOURCE_NAMES.get(track.source, ""),
                thumbnail_url=track.cover_url,
                input_message_content=InputTextMessageContent(
                    message_text=f"🎵 {title}\n⏳ Downloading…"
                ),
                reply_markup=_wait_keyboard(deps),
            )
        )
    return results


def _tiktok_result(deps: Deps, url: str) -> InlineQueryResultArticle:
    rid = f"tt:{_short_hash(url)}"
    deps.inline.put(rid, InlineRef(tiktok_url=url))
    return InlineQueryResultArticle(
        id=rid,
        title=messages.INLINE_TIKTOK_TITLE,
        description=messages.INLINE_TIKTOK_DESC,
        input_message_content=InputTextMessageContent(
            message_text=f"🎬 {messages.INLINE_TIKTOK_TITLE}\n⏳ Downloading…"
        ),
        reply_markup=_wait_keyboard(deps),
    )


def _result_id(deps: Deps, text: str, track: Track, index: int) -> str:
    """A unique inline-result id that doubles as the placeholder cache key.

    Real tracks have a stable ``source:id`` (globally unique). Query-only items
    (Spotify/Apple) have synthetic ids, so we key them by the query text + index
    to stay unique within this answer and distinct from other inline queries."""
    if track.url:
        return deps.files.key(track.source, track.id)
    return f"q{index}:{_short_hash(text)}"


def _wait_keyboard(deps: Deps) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏳ Downloading…",
                    url=f"https://t.me/{deps.bot_username}",
                )
            ]
        ]
    )


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


# Telegram returns one of these when the inline message no longer exists — the
# user deleted it after picking the placeholder. We treat that as a cancellation
# (silent no-op) rather than an error: there's nothing left to edit.
_DELETED_MARKERS = (
    "message_id_invalid",
    "message to edit not found",
    "message can't be edited",
)


def _message_deleted(exc: TelegramBadRequest) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _DELETED_MARKERS)


# --- chosen-result completion --------------------------------------------


async def _finish_audio(
    deps: Deps, bot: Bot, track: Track, inline_message_id: str, user_id: int
) -> None:
    file_id = await delivery.ensure_file_id(
        deps, bot, track, inline_message_id, user_id
    )
    if not file_id:
        return
    title = formatting.display_title(track)
    try:
        await bot.edit_message_media(
            media=InputMediaAudio(
                media=file_id,
                title=track.title,
                performer=track.uploader,
            ),
            inline_message_id=inline_message_id,
        )
    except TelegramBadRequest as exc:
        if _message_deleted(exc):
            return  # user deleted the inline message — request cancelled
        logger.exception("Failed to embed inline audio")
        await safe_inline_edit(bot, inline_message_id, f"🎵 {title}\n{track.url}")


async def _finish_video(
    deps: Deps, bot: Bot, url: str, inline_message_id: str, user_id: int
) -> None:
    metrics.incr("tiktok_videos")
    file_id = await delivery.ensure_video_file_id(
        deps, bot, url, inline_message_id, user_id
    )
    if not file_id:
        return
    try:
        await bot.edit_message_media(
            media=InputMediaVideo(media=file_id),
            inline_message_id=inline_message_id,
        )
    except TelegramBadRequest as exc:
        if _message_deleted(exc):
            return  # user deleted the inline message — request cancelled
        logger.exception("Failed to embed inline video")
        await safe_inline_edit(bot, inline_message_id, f"🎬 {url}")
