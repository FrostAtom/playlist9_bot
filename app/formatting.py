"""Presentation helpers: turn domain objects into Telegram-ready output."""
from __future__ import annotations

from typing import List, Tuple

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import messages
from .models import Track

# callback_data is limited to 64 bytes, so we reference a track by a short
# token (the results message id) plus its index instead of a full URL.
PICK_PREFIX = "p:"
PAGE_PREFIX = "g:"
TOGGLE_PREFIX = "t:"

# Practical cap for an inline button caption.
MAX_LABEL = 60

# Small badge per source so the user sees where each result comes from.
SOURCE_BADGES = {
    "youtube": "🎵",
    "soundcloud": "☁️",
}

SOURCE_NAMES = {
    "youtube": "YouTube Music",
    "soundcloud": "SoundCloud",
}

# Shortened names for the (cramped) toggle button.
SOURCE_SHORT = {
    "youtube": "YT Music",
    "soundcloud": "SoundCloud",
}


def source_label(source: str) -> str:
    badge = SOURCE_BADGES.get(source, "")
    name = SOURCE_NAMES.get(source, source)
    return f"{badge} {name}".strip()


def source_short(source: str) -> str:
    badge = SOURCE_BADGES.get(source, "")
    name = SOURCE_SHORT.get(source, source)
    return f"{badge} {name}".strip()


def track_label(track: Track) -> str:
    # No source badge here: pick buttons show just the clean "Artist — Title".
    label = f"{track.uploader} — {track.title}" if track.uploader else track.title
    if len(label) > MAX_LABEL:
        label = label[: MAX_LABEL - 1].rstrip() + "…"
    return label


def results_page(
    tracks: List[Track],
    page: int,
    per_page: int,
    token: str,
    source: str,
) -> Tuple[str, InlineKeyboardMarkup]:
    """Build the message body and inline keyboard for a single page.

    The control row carries the page arrows (only when there is more than one
    page) around a button that toggles the search source.
    """
    total = len(tracks)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(0, min(page, pages - 1))
    start = page * per_page
    chunk = tracks[start : start + per_page]

    rows = [
        [
            InlineKeyboardButton(
                text=track_label(track),
                callback_data=f"{PICK_PREFIX}{token}:{start + offset}",
            )
        ]
        for offset, track in enumerate(chunk)
    ]

    # Arrows are shown only when there is somewhere to go; the toggle is always
    # present in the middle.
    control = []
    if page > 0:
        control.append(
            InlineKeyboardButton(
                text="◀", callback_data=f"{PAGE_PREFIX}{token}:{page - 1}"
            )
        )
    control.append(
        InlineKeyboardButton(
            text=f"{source_short(source)} ⇄", callback_data=f"{TOGGLE_PREFIX}{token}"
        )
    )
    if page < pages - 1:
        control.append(
            InlineKeyboardButton(
                text="▶", callback_data=f"{PAGE_PREFIX}{token}:{page + 1}"
            )
        )
    rows.append(control)

    body = messages.results_header(total, page, pages, source_label(source))
    return body, InlineKeyboardMarkup(inline_keyboard=rows)
