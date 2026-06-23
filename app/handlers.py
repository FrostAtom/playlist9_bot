"""aiogram router: search, pagination, inline mode and download handlers."""
from __future__ import annotations

import logging
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InputMediaAudio,
    InputTextMessageContent,
    Message,
)

from . import formatting, messages
from .config import Settings
from .limiter import DownloadLimiter
from .models import Track
from .service import MusicService

logger = logging.getLogger(__name__)

# How many recent searches to keep per user (maps a results message to its
# track list, so pagination/selection works without re-querying).
SEARCH_CACHE_SIZE = 20
INLINE_RESULTS = 20
BOT_USERNAME = "atomsdungeon_bot"


@dataclass
class SearchState:
    """A search whose results are paginated and can be re-run on another source."""

    query: str
    source: str
    tracks: List[Track]


class SearchCache:
    """Per-user store of recent searches, keyed by results message id."""

    def __init__(self, per_user: int = SEARCH_CACHE_SIZE) -> None:
        self._per_user = per_user
        self._data: Dict[int, "OrderedDict[str, SearchState]"] = {}

    def save(self, user_id: int, token: str, state: SearchState) -> None:
        store = self._data.setdefault(user_id, OrderedDict())
        store[token] = state
        while len(store) > self._per_user:
            store.popitem(last=False)

    def load(self, user_id: int, token: str) -> Optional[SearchState]:
        return self._data.get(user_id, {}).get(token)


class History:
    """Tracks bot/user message ids per chat so /clear can wipe everything
    except the delivered audio tracks (which are never recorded)."""

    def __init__(self, capacity: int = 300) -> None:
        self._cap = capacity
        self._data: Dict[int, List[int]] = {}

    def add(self, chat_id: int, message_id: int) -> None:
        ids = self._data.setdefault(chat_id, [])
        ids.append(message_id)
        # Bound memory for chats that never call /clear; oldest ids are also
        # the least likely to still be deletable (Telegram's 48h limit).
        if len(ids) > self._cap:
            del ids[: len(ids) - self._cap]

    def discard(self, chat_id: int, message_id: int) -> None:
        ids = self._data.get(chat_id)
        if ids and message_id in ids:
            ids.remove(message_id)

    def pop_all(self, chat_id: int) -> List[int]:
        return self._data.pop(chat_id, [])


class FileIdCache:
    """Remembers Telegram file_ids of sent tracks so inline mode can re-send
    them instantly (as playable audio) without re-downloading."""

    def __init__(self, capacity: int = 500) -> None:
        self._cap = capacity
        self._data: "OrderedDict[str, str]" = OrderedDict()

    @staticmethod
    def key(source: str, track_id: str) -> str:
        return f"{source}:{track_id}"

    def get(self, key: str) -> Optional[str]:
        return self._data.get(key)

    def put(self, key: str, file_id: str) -> None:
        self._data[key] = file_id
        self._data.move_to_end(key)
        while len(self._data) > self._cap:
            self._data.popitem(last=False)


class TrackCache:
    """Short-lived store of tracks offered inline, keyed by inline result id,
    so a chosen result can be downloaded and delivered."""

    def __init__(self, capacity: int = 1000) -> None:
        self._cap = capacity
        self._data: "OrderedDict[str, Track]" = OrderedDict()

    def put(self, key: str, track: Track) -> None:
        self._data[key] = track
        self._data.move_to_end(key)
        while len(self._data) > self._cap:
            self._data.popitem(last=False)

    def get(self, key: str) -> Optional[Track]:
        return self._data.get(key)


@dataclass
class Deps:
    settings: Settings
    service: MusicService
    limiter: DownloadLimiter
    cache: SearchCache
    history: History
    files: FileIdCache
    inline: TrackCache


def _parse(data: str) -> Tuple[str, Optional[int]]:
    # "<prefix><token>:<number>" -> (token, number)
    _, payload = data.split(":", 1)
    token, _, raw = payload.rpartition(":")
    try:
        return token, int(raw)
    except ValueError:
        return token, None


def _display_title(track: Track) -> str:
    return f"{track.uploader} — {track.title}" if track.uploader else track.title


async def _safe_edit(message: Message, text: str, **kwargs) -> None:
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest:
        pass


def build_router(deps: Deps) -> Router:
    router = Router()

    @router.message(Command("start", "help"))
    async def start(message: Message) -> None:
        deps.history.add(message.chat.id, message.message_id)
        sent = await message.answer(messages.WELCOME, parse_mode=ParseMode.MARKDOWN)
        deps.history.add(message.chat.id, sent.message_id)

    @router.message(Command("clear"))
    async def clear(message: Message) -> None:
        chat_id = message.chat.id
        ids = deps.history.pop_all(chat_id)
        ids.append(message.message_id)
        for message_id in ids:
            try:
                await message.bot.delete_message(chat_id, message_id)
            except TelegramBadRequest:
                pass  # too old / already gone / not deletable

    @router.message(F.text & ~F.text.startswith("/"))
    async def on_text(message: Message) -> None:
        deps.history.add(message.chat.id, message.message_id)
        text = message.text.strip()

        match = deps.service.resolve(text)
        if match:
            status = await message.answer(messages.DOWNLOADING)
            deps.history.add(message.chat.id, status.message_id)
            await _deliver(deps, message.chat.id, message.from_user.id, match[1], status)
            return

        status = await message.answer(messages.searching(text))
        deps.history.add(message.chat.id, status.message_id)
        source = deps.service.default_source()
        try:
            tracks = await deps.service.search(text, deps.settings.max_results, source)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Search failed")
            await _safe_edit(status, messages.search_failed(exc))
            return

        if not tracks:
            await _safe_edit(status, messages.NOT_FOUND)
            return

        token = str(status.message_id)
        deps.cache.save(message.from_user.id, token, SearchState(text, source, tracks))
        body, keyboard = formatting.results_page(
            tracks, 0, deps.settings.results_per_page, token, source
        )
        await _safe_edit(status, body, reply_markup=keyboard)

    @router.callback_query(F.data.startswith(formatting.PICK_PREFIX))
    async def on_pick(callback: CallbackQuery) -> None:
        token, index = _parse(callback.data)
        state = deps.cache.load(callback.from_user.id, token)
        if state is None or index is None or index >= len(state.tracks):
            await callback.answer(messages.RESULTS_EXPIRED, show_alert=True)
            return

        await callback.answer()
        track = state.tracks[index]
        # Keep the results message (and its keyboard) intact so the user can
        # pick more tracks; download progress goes into a fresh message.
        status = await callback.message.answer(messages.DOWNLOADING_CHOICE)
        deps.history.add(callback.message.chat.id, status.message_id)
        await _deliver(
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
        await _safe_edit(callback.message, body, reply_markup=keyboard)

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
        await _safe_edit(callback.message, body, reply_markup=keyboard)

    @router.inline_query()
    async def inline(query: InlineQuery) -> None:
        text = query.query.strip()
        if not text:
            await query.answer([], cache_time=5, is_personal=True)
            return

        source = deps.service.default_source()
        try:
            tracks = await deps.service.search(text, INLINE_RESULTS, source)
        except Exception:  # noqa: BLE001
            logger.exception("Inline search failed")
            await query.answer([], cache_time=5)
            return

        results = []
        for track in tracks[:INLINE_RESULTS]:
            key = deps.files.key(track.source, track.id)
            title = _display_title(track)
            file_id = deps.files.get(key)
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
                                    url=f"https://t.me/{BOT_USERNAME}",
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
            await _safe_inline_edit(
                bot, inline_message_id, messages.RESULTS_EXPIRED
            )
            return

        file_id = await _ensure_file_id(
            deps, bot, track, inline_message_id, chosen.from_user.id
        )
        if not file_id:
            return

        title = _display_title(track)
        try:
            await bot.edit_message_media(
                media=InputMediaAudio(
                    media=file_id,
                    title=track.title,
                    performer=track.uploader,
                ),
                inline_message_id=inline_message_id,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=f"🎵 via @{BOT_USERNAME}",
                                url=f"https://t.me/{BOT_USERNAME}",
                            )
                        ]
                    ]
                ),
            )
        except TelegramBadRequest:
            logger.exception("Failed to embed inline audio")
            await _safe_inline_edit(
                bot, inline_message_id, f"🎵 {title}\n{track.url}"
            )

    return router


async def _safe_inline_edit(bot: Bot, inline_message_id: str, text: str) -> None:
    try:
        await bot.edit_message_text(text, inline_message_id=inline_message_id)
    except TelegramBadRequest:
        pass


async def _ensure_file_id(
    deps: Deps, bot: Bot, track: Track, inline_message_id: str, user_id: int
) -> Optional[str]:
    """Return a Telegram file_id for the track, downloading + uploading to the
    storage chat if needed. Edits the inline message with an error otherwise."""
    key = deps.files.key(track.source, track.id)
    file_id = deps.files.get(key)
    if file_id:
        return file_id

    if not deps.settings.storage_chat_id:
        title = _display_title(track)
        await _safe_inline_edit(bot, inline_message_id, f"🎵 {title}\n{track.url}")
        return None

    try:
        async with deps.limiter.slot(user_id):
            with tempfile.TemporaryDirectory() as workdir:
                audio = await deps.service.download(track, workdir)
                if not audio.exists:
                    await _safe_inline_edit(bot, inline_message_id, messages.NO_AUDIO)
                    return None
                if audio.size > deps.settings.max_file_size:
                    await _safe_inline_edit(
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
        await _safe_inline_edit(bot, inline_message_id, messages.SEARCH_ERROR)
        return None

    if not sent.audio:
        return None
    deps.files.put(key, sent.audio.file_id)
    return sent.audio.file_id


async def _deliver(
    deps: Deps,
    chat_id: int,
    user_id: int,
    ref: Union[Track, str],
    status: Message,
) -> None:
    bot = status.bot

    queued = deps.limiter.busy(user_id)
    if queued:
        await _safe_edit(status, messages.QUEUED)

    async with deps.limiter.slot(user_id):
        if queued:
            await _safe_edit(status, messages.DOWNLOADING)
        with tempfile.TemporaryDirectory() as workdir:
            try:
                audio = await deps.service.download(ref, workdir)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Download failed")
                await _safe_edit(status, messages.download_failed(exc))
                return

            if not audio.exists:
                await _safe_edit(status, messages.NO_AUDIO)
                return

            if audio.size > deps.settings.max_file_size:
                await _safe_edit(
                    status,
                    messages.too_large(audio.size, deps.settings.max_file_size),
                )
                return

            await _safe_edit(status, messages.UPLOADING)
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
                    deps.files.put(
                        deps.files.key(ref.source, ref.id), sent.audio.file_id
                    )
                await status.delete()
                deps.history.discard(chat_id, status.message_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Send failed")
                await _safe_edit(status, messages.send_failed(exc))
