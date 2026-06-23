"""aiogram router: search, pagination, inline mode and download handlers.

This module is intentionally thin — it parses updates and decides *what* to do;
the *how* lives elsewhere: download/send in ``delivery``, state in ``caches``,
the dependency bundle in ``deps``, and aiogram-safe wrappers in ``tg_utils``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Tuple

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InputMediaAudio,
    InputTextMessageContent,
    Message,
)

from . import delivery, external_links, formatting, messages
from .caches import SearchState
from .deps import Deps
from .tg_utils import delete_after, safe_delete, safe_edit, safe_inline_edit

logger = logging.getLogger(__name__)


def _parse(data: str) -> Tuple[str, Optional[int]]:
    # "<prefix><token>:<number>" -> (token, number)
    _, payload = data.split(":", 1)
    token, _, raw = payload.rpartition(":")
    try:
        return token, int(raw)
    except ValueError:
        return token, None


def build_router(deps: Deps) -> Router:
    router = Router()

    @router.message(Command("start", "help"))
    async def start(message: Message) -> None:
        await message.answer(messages.WELCOME, parse_mode=ParseMode.MARKDOWN)

    @router.message(F.text & ~F.text.startswith("/"))
    async def on_text(message: Message) -> None:
        text = message.text.strip()
        user_id = message.from_user.id
        # The user's request is removed right away; bot replies stand on their own.
        await safe_delete(message)

        match = deps.service.resolve(text)
        # Spotify / Apple Music links can't be downloaded directly; we recognize
        # them so we can resolve a query and find the track on YouTube Music.
        external_url = None if match else external_links.detect(text)

        if match or external_url:
            if not deps.rate.allow(user_id):
                notice = await message.answer(
                    messages.rate_limited(
                        deps.settings.rate_per_minute,
                        deps.rate.retry_after(user_id),
                    )
                )
                delete_after(notice, 15)
                return
            if match:
                status = await message.answer(messages.DOWNLOADING)
                await delivery.deliver(deps, message.chat.id, user_id, match[1], status)
                return
            status = await message.answer(messages.RESOLVING_LINK)
            external = await asyncio.to_thread(external_links.resolve, text)
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
            await delivery.deliver(deps, message.chat.id, user_id, tracks[0], status)
            return

        status = await message.answer(messages.searching(text))
        # The results message is ephemeral — it auto-deletes after a few minutes.
        delete_after(status, deps.settings.results_ttl)
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
        status = await callback.message.answer(messages.DOWNLOADING_CHOICE)
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
        body, keyboard = formatting.results_page(
            state.tracks, page, deps.settings.results_per_page, token, state.source
        )
        await safe_edit(callback.message, body, reply_markup=keyboard)

    @router.callback_query(F.data.startswith(formatting.TOGGLE_PREFIX))
    async def on_toggle(callback: CallbackQuery) -> None:
        token = callback.data[len(formatting.TOGGLE_PREFIX):]
        state = deps.cache.load(callback.from_user.id, token)
        if state is None:
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

    @router.inline_query()
    async def inline(query: InlineQuery) -> None:
        text = query.query.strip()
        if not text:
            await query.answer([], cache_time=5, is_personal=True)
            return

        limit = deps.settings.inline_results
        source = deps.service.default_source()
        try:
            tracks = await deps.service.search(text, limit, source)
        except Exception:  # noqa: BLE001
            logger.exception("Inline search failed")
            await query.answer([], cache_time=5)
            return

        # One round-trip to resolve which results are already on Telegram.
        keys = [deps.files.key(t.source, t.id) for t in tracks[:limit]]
        cached = await deps.files.get_many(keys)
        results = []
        for track in tracks[:limit]:
            key = deps.files.key(track.source, track.id)
            title = formatting.display_title(track)
            file_id = cached.get(key)
            if file_id:
                # Already on Telegram's servers — send it as playable audio.
                results.append(
                    InlineQueryResultCachedAudio(id=key, audio_file_id=file_id)
                )
                continue
            # Offer a placeholder; the file is downloaded once the user picks it
            # (handled in chosen_inline_result). A keyboard is required so that
            # Telegram returns an inline_message_id we can edit into audio.
            deps.inline.put(key, track)
            results.append(
                InlineQueryResultArticle(
                    id=key,
                    title=title,
                    description=track.album or formatting.SOURCE_NAMES.get(track.source, ""),
                    thumbnail_url=track.cover_url,
                    input_message_content=InputTextMessageContent(
                        message_text=f"🎵 {title}\n⏳ Downloading…"
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="⏳ Downloading…",
                                    url=f"https://t.me/{deps.bot_username}",
                                )
                            ]
                        ]
                    ),
                )
            )
        # cache_time=0: results depend on per-track cache state, don't cache.
        await query.answer(results, cache_time=0, is_personal=True)

    @router.chosen_inline_result()
    async def on_chosen(chosen: ChosenInlineResult, bot: Bot) -> None:
        inline_message_id = chosen.inline_message_id
        if not inline_message_id:
            return  # cached-audio results need no follow-up
        track = deps.inline.get(chosen.result_id)
        if track is None:
            await safe_inline_edit(bot, inline_message_id, messages.RESULTS_EXPIRED)
            return

        file_id = await delivery.ensure_file_id(
            deps, bot, track, inline_message_id, chosen.from_user.id
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
        except TelegramBadRequest:
            logger.exception("Failed to embed inline audio")
            await safe_inline_edit(bot, inline_message_id, f"🎵 {title}\n{track.url}")

    return router
