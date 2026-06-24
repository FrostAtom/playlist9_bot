"""aiogram router: search, pagination, inline mode and download handlers.

This module is intentionally thin — it parses updates and decides *what* to do;
the *how* lives elsewhere: download/send in ``delivery``, state in ``caches``,
the dependency bundle in ``deps``, and aiogram-safe wrappers in ``tg_utils``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Tuple

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    InlineQuery,
    Message,
)

from . import delivery, formatting, inline, messages
from .caches import QUERY_PLAYLIST, URL_PLAYLIST, PendingLink, SearchState
from .deps import Deps
from .telegram import answer_ephemeral, safe_delete, safe_edit
from ..infra.metrics import metrics
from ..models import Track
from ..music import links, resolver
from ..music.resolver import InputKind

logger = logging.getLogger(__name__)


def _parse(data: str) -> Tuple[str, Optional[int]]:
    # "<prefix><token>:<number>" -> (token, number)
    _, payload = data.split(":", 1)
    token, _, raw = payload.rpartition(":")
    try:
        return token, int(raw)
    except ValueError:
        return token, None


async def _render_playlist(
    deps: Deps, status: Message, user_id: int, state: SearchState
) -> None:
    """Cache a playlist's tracks and show its first page in ``status``."""
    metrics.incr("playlists")
    token = str(status.message_id)
    deps.cache.save(user_id, token, state)
    body, keyboard = formatting.playlist_page(
        state.tracks, 0, deps.settings.results_per_page, token, state.title or "Playlist"
    )
    await safe_edit(status, body, reply_markup=keyboard)


async def _show_url_playlist(
    deps: Deps, status: Message, source: str, url: str, user_id: int
) -> None:
    """Enumerate a YouTube/SoundCloud playlist and render it (direct downloads)."""
    try:
        tracks, title = await deps.service.playlist(
            url, deps.settings.playlist_limit, source
        )
    except Exception:  # noqa: BLE001
        logger.exception("Playlist load failed")
        await safe_edit(status, messages.PLAYLIST_FAILED)
        return
    if not tracks:
        await safe_edit(status, messages.PLAYLIST_EMPTY)
        return
    await _render_playlist(
        deps,
        status,
        user_id,
        SearchState(
            query="", source=source, tracks=tracks, kind=URL_PLAYLIST, title=title
        ),
    )


async def _show_query_playlist(
    deps: Deps, status: Message, text: str, user_id: int
) -> None:
    """Scrape a Spotify/Apple playlist into searchable items and render it."""
    playlist = await asyncio.to_thread(
        links.resolve_playlist, text, deps.settings.playlist_limit
    )
    if playlist is None or not playlist.items:
        await safe_edit(status, messages.PLAYLIST_FAILED)
        return
    source = deps.service.default_source()
    tracks = resolver.external_items_to_tracks(playlist.items, source)
    await _render_playlist(
        deps,
        status,
        user_id,
        SearchState(
            query="", source=source, tracks=tracks,
            kind=QUERY_PLAYLIST, title=playlist.name,
        ),
    )


async def _deliver_query_item(
    deps: Deps, chat_id: int, user_id: int, track: Track, status: Message
) -> None:
    """Pick for a Spotify/Apple playlist item: search, then send the top match."""
    query = track.query or formatting.display_title(track)
    try:
        matches = await deps.service.search(query, 1, track.source)
    except Exception:  # noqa: BLE001
        logger.exception("Playlist item search failed")
        await safe_edit(status, messages.SEARCH_ERROR)
        return
    if not matches:
        await safe_edit(status, messages.NOT_FOUND)
        return
    await delivery.deliver(deps, chat_id, user_id, matches[0], status)


class _ActivityMiddleware(BaseMiddleware):
    """Record the acting user on every update, for the unique-user metric.

    Sits as an outer middleware on the user-facing observers so it runs once per
    update regardless of which handler (if any) ends up matching."""

    async def __call__(self, handler, event, data):  # type: ignore[override]
        user = data.get("event_from_user")
        if user is not None:
            metrics.seen_user(user.id)
        return await handler(event, data)


def build_router(deps: Deps) -> Router:
    router = Router()

    activity = _ActivityMiddleware()
    for observer in (
        router.message,
        router.callback_query,
        router.inline_query,
        router.chosen_inline_result,
    ):
        observer.outer_middleware(activity)

    @router.message(Command("start", "help"))
    async def start(message: Message) -> None:
        await message.answer(messages.WELCOME, parse_mode=ParseMode.MARKDOWN)

    @router.message(F.text & ~F.text.startswith("/"))
    async def on_text(message: Message) -> None:
        text = message.text.strip()
        user_id = message.from_user.id
        # The user's request is removed right away; bot replies stand on their own.
        await safe_delete(message)

        async def _rate_notice() -> None:
            metrics.incr("rate_limited")
            await answer_ephemeral(
                message,
                messages.rate_limited(
                    deps.settings.rate_per_minute, deps.rate.retry_after(user_id)
                ),
                15,
            )

        # Classify the input once (TikTok / YT-SC link / external link / search);
        # the decision tree is shared with the web entry point (music.resolver).
        info = resolver.classify(deps.service, text)
        kind = info.kind

        # A TikTok link: download the clip and send it back as a video (not the
        # MP3 audio pipeline).
        if kind is InputKind.TIKTOK:
            if not deps.rate.allow(user_id):
                await _rate_notice()
                return
            metrics.incr("tiktok_videos")
            status = await answer_ephemeral(
                message, messages.DOWNLOADING_VIDEO, deps.settings.results_ttl
            )
            await delivery.deliver_video(
                deps, message.chat.id, user_id, info.tiktok_url, status
            )
            return

        # A YouTube/SoundCloud link that's both a track and a playlist — ask which.
        if kind is InputKind.LINK_AMBIGUOUS:
            prompt = await answer_ephemeral(
                message, messages.PLAYLIST_PROMPT, deps.settings.results_ttl
            )
            token = str(prompt.message_id)
            deps.links.save(
                user_id,
                token,
                PendingLink(info.source, info.track_url, info.playlist_url),
            )
            await safe_edit(
                prompt,
                messages.PLAYLIST_PROMPT,
                reply_markup=formatting.playlist_prompt_keyboard(token),
            )
            return

        # A YouTube/SoundCloud playlist or set.
        if kind is InputKind.LINK_PLAYLIST:
            status = await answer_ephemeral(
                message, messages.LOADING_PLAYLIST, deps.settings.results_ttl
            )
            await _show_url_playlist(
                deps, status, info.source, info.playlist_url, user_id
            )
            return

        # A YouTube/SoundCloud single track: download it directly.
        if kind is InputKind.LINK_TRACK:
            if not deps.rate.allow(user_id):
                await _rate_notice()
                return
            status = await answer_ephemeral(
                message, messages.DOWNLOADING, deps.settings.results_ttl
            )
            target = info.track_url or info.link_url
            await delivery.deliver(deps, message.chat.id, user_id, target, status)
            return

        # A Spotify / Apple Music playlist link: scrape its tracks and offer each
        # as a button that searches YouTube Music on tap.
        if kind is InputKind.EXTERNAL_PLAYLIST:
            status = await answer_ephemeral(
                message, messages.LOADING_PLAYLIST, deps.settings.results_ttl
            )
            await _show_query_playlist(deps, status, text, user_id)
            return

        if kind is InputKind.EXTERNAL_TRACK:
            if not deps.rate.allow(user_id):
                await _rate_notice()
                return
            status = await answer_ephemeral(
                message, messages.RESOLVING_LINK, deps.settings.results_ttl
            )
            external = await asyncio.to_thread(links.resolve, text)
            if external is None:
                await safe_edit(status, messages.LINK_FAILED)
                return
            source = deps.service.default_source()
            try:
                tracks = await deps.service.search(
                    external.query, deps.settings.max_results, source
                )
            except Exception:  # noqa: BLE001
                logger.exception("Link search failed")
                await safe_edit(status, messages.SEARCH_ERROR)
                return
            if not tracks:
                await safe_edit(status, messages.link_not_found(external.provider))
                return
            metrics.incr("links_resolved")
            await delivery.deliver(deps, message.chat.id, user_id, tracks[0], status)
            return

        metrics.incr("searches")
        # The results message is ephemeral — it auto-deletes after a few minutes.
        status = await answer_ephemeral(
            message, messages.searching(text), deps.settings.results_ttl
        )
        source = deps.service.default_source()
        try:
            tracks = await deps.service.search(text, deps.settings.max_results, source)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Search failed")
            await safe_edit(status, messages.search_failed(exc))
            return

        if not tracks:
            await safe_edit(status, messages.NOT_FOUND)
            return

        token = str(status.message_id)
        deps.cache.save(message.from_user.id, token, SearchState(text, source, tracks))
        body, keyboard = formatting.results_page(
            tracks, 0, deps.settings.results_per_page, token, source
        )
        await safe_edit(status, body, reply_markup=keyboard)

    @router.callback_query(F.data.startswith(formatting.PICK_PREFIX))
    async def on_pick(callback: CallbackQuery) -> None:
        token, index = _parse(callback.data)
        state = deps.cache.load(callback.from_user.id, token)
        if state is None or index is None or index >= len(state.tracks):
            await callback.answer(messages.RESULTS_EXPIRED, show_alert=True)
            return

        if not deps.rate.allow(callback.from_user.id):
            metrics.incr("rate_limited")
            await callback.answer(
                messages.rate_limited(
                    deps.settings.rate_per_minute,
                    deps.rate.retry_after(callback.from_user.id),
                ),
                show_alert=True,
            )
            return

        await callback.answer()
        track = state.tracks[index]
        # Keep the results message (and its keyboard) intact so the user can
        # pick more tracks; download progress goes into a fresh message.
        status = await answer_ephemeral(
            callback.message, messages.DOWNLOADING_CHOICE, deps.settings.results_ttl
        )
        if state.kind == QUERY_PLAYLIST:
            # Spotify/Apple item: no direct URL — find it on YouTube Music first.
            await _deliver_query_item(
                deps, callback.message.chat.id, callback.from_user.id, track, status
            )
        else:
            await delivery.deliver(
                deps, callback.message.chat.id, callback.from_user.id, track, status
            )

    @router.callback_query(F.data.startswith(formatting.PAGE_PREFIX))
    async def on_page(callback: CallbackQuery) -> None:
        token, page = _parse(callback.data)
        state = deps.cache.load(callback.from_user.id, token)
        if state is None or page is None:
            await callback.answer(messages.RESULTS_EXPIRED, show_alert=True)
            return

        await callback.answer()
        if state.is_playlist:
            body, keyboard = formatting.playlist_page(
                state.tracks, page, deps.settings.results_per_page,
                token, state.title or "Playlist",
            )
        else:
            body, keyboard = formatting.results_page(
                state.tracks, page, deps.settings.results_per_page, token, state.source
            )
        await safe_edit(callback.message, body, reply_markup=keyboard)

    @router.callback_query(F.data.startswith(formatting.TOGGLE_PREFIX))
    async def on_toggle(callback: CallbackQuery) -> None:
        token = callback.data[len(formatting.TOGGLE_PREFIX):]
        state = deps.cache.load(callback.from_user.id, token)
        if state is None or state.is_playlist:
            # Playlists have no source toggle; ignore a stale/foreign callback.
            await callback.answer(messages.RESULTS_EXPIRED, show_alert=True)
            return

        new_source = deps.service.next_source(state.source)
        try:
            tracks = await deps.service.search(
                state.query, deps.settings.max_results, new_source
            )
        except Exception:  # noqa: BLE001
            logger.exception("Toggle search failed")
            await callback.answer(messages.SEARCH_ERROR, show_alert=True)
            return

        if not tracks:
            await callback.answer(
                messages.nothing_on(formatting.source_label(new_source)),
                show_alert=True,
            )
            return

        await callback.answer()
        deps.cache.save(
            callback.from_user.id, token, SearchState(state.query, new_source, tracks)
        )
        body, keyboard = formatting.results_page(
            tracks, 0, deps.settings.results_per_page, token, new_source
        )
        await safe_edit(callback.message, body, reply_markup=keyboard)

    @router.callback_query(F.data.startswith(formatting.PLAYLIST_ONE_PREFIX))
    async def on_playlist_one(callback: CallbackQuery) -> None:
        token = callback.data[len(formatting.PLAYLIST_ONE_PREFIX):]
        pending = deps.links.load(callback.from_user.id, token)
        if pending is None:
            await callback.answer(messages.RESULTS_EXPIRED, show_alert=True)
            return
        if not deps.rate.allow(callback.from_user.id):
            metrics.incr("rate_limited")
            await callback.answer(
                messages.rate_limited(
                    deps.settings.rate_per_minute,
                    deps.rate.retry_after(callback.from_user.id),
                ),
                show_alert=True,
            )
            return
        await callback.answer()
        await safe_edit(callback.message, messages.DOWNLOADING)
        await delivery.deliver(
            deps,
            callback.message.chat.id,
            callback.from_user.id,
            pending.track_url,
            callback.message,
        )

    @router.callback_query(F.data.startswith(formatting.PLAYLIST_ALL_PREFIX))
    async def on_playlist_all(callback: CallbackQuery) -> None:
        token = callback.data[len(formatting.PLAYLIST_ALL_PREFIX):]
        pending = deps.links.load(callback.from_user.id, token)
        if pending is None:
            await callback.answer(messages.RESULTS_EXPIRED, show_alert=True)
            return
        await callback.answer()
        await safe_edit(callback.message, messages.LOADING_PLAYLIST)
        await _show_url_playlist(
            deps,
            callback.message,
            pending.source,
            pending.playlist_url,
            callback.from_user.id,
        )

    @router.inline_query()
    async def on_inline(query: InlineQuery) -> None:
        # Inline accepts a search OR a pasted track/playlist/TikTok link; the
        # classification + result building lives in the inline module.
        await inline.answer(deps, query)

    @router.chosen_inline_result()
    async def on_chosen(chosen: ChosenInlineResult, bot: Bot) -> None:
        await inline.handle_chosen(deps, bot, chosen)

    return router
