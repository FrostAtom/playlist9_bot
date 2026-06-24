"""Classify a pasted/typed input into *what kind of thing* it is.

Both entry points that accept free-form user input — the Telegram bot's
``on_text`` handler and the web ``/api/search`` endpoint — face the same first
question: is this a TikTok link, a YouTube/SoundCloud track or playlist, a
Spotify/Apple track or playlist/album, or just a plain search query? That
decision tree was duplicated in both places; this module is the single source of
truth for it.

:func:`classify` is intentionally **pure and synchronous** — it only inspects
the text with regex/URL matchers (no network, no downloads), so the two callers
can branch on its result and then perform their own (differing) side effects:
the bot auto-downloads a single match and sends Telegram messages, while the web
returns a list of tracks for the browser to render. :func:`external_items_to_tracks`
likewise centralises the one-shape-fits-both conversion of a scraped
Spotify/Apple collection into query-backed :class:`~app.models.Track` objects.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import List, Optional

from ..models import Track
from . import links
from .links import ExternalItem
from .service import MusicService
from .video import detect_tiktok


class InputKind(enum.Enum):
    """What a piece of user input resolves to (before any network call)."""

    TIKTOK = "tiktok"                      #: a TikTok post (video / photo)
    LINK_TRACK = "link_track"             #: a YouTube/SoundCloud single track
    LINK_PLAYLIST = "link_playlist"       #: a YouTube/SoundCloud playlist / set
    LINK_AMBIGUOUS = "link_ambiguous"     #: a YT watch?v=…&list=… (track *and* list)
    EXTERNAL_TRACK = "external_track"     #: a Spotify/Apple single track
    EXTERNAL_PLAYLIST = "external_playlist"  #: a Spotify/Apple playlist / album
    SEARCH = "search"                     #: plain text to search


@dataclass(frozen=True)
class ClassifiedInput:
    """The outcome of :func:`classify`: the kind plus any extracted URLs.

    The fields populated depend on ``kind``:

    * ``TIKTOK`` → ``tiktok_url``
    * ``LINK_*`` → ``source`` and ``track_url`` / ``playlist_url`` (as available)
    * ``EXTERNAL_*`` → ``external_url`` (the canonical Spotify/Apple link)
    * ``SEARCH`` → nothing beyond ``text``
    """

    kind: InputKind
    text: str
    source: Optional[str] = None
    track_url: Optional[str] = None
    playlist_url: Optional[str] = None
    #: The canonical URL the source matched (``match[1]`` from
    #: :meth:`MusicService.resolve`); a sensible download target for a
    #: ``LINK_TRACK`` whose ``track_url`` couldn't be narrowed down.
    link_url: Optional[str] = None
    tiktok_url: Optional[str] = None
    external_url: Optional[str] = None


def classify(service: MusicService, text: str) -> ClassifiedInput:
    """Decide what ``text`` is, mirroring the bot's ``on_text`` priority order.

    Priority (highest first): TikTok post → YouTube/SoundCloud link (track,
    playlist, or ambiguous) → Spotify/Apple playlist/album → Spotify/Apple track
    → plain search.
    """
    text = text.strip()

    # 1. A TikTok post is a direct link checked before anything else.
    tiktok_url = detect_tiktok(text)
    if tiktok_url:
        return ClassifiedInput(InputKind.TIKTOK, text, tiktok_url=tiktok_url)

    # 2. A YouTube / SoundCloud link the registered sources recognise.
    match = service.resolve(text)
    if match:
        info = service.link_info(text)
        track_url = info.track_url if info else None
        playlist_url = info.playlist_url if info else None
        source = match[0].name
        if track_url and playlist_url:
            kind = InputKind.LINK_AMBIGUOUS
        elif playlist_url:
            kind = InputKind.LINK_PLAYLIST
        else:
            kind = InputKind.LINK_TRACK
        return ClassifiedInput(
            kind,
            text,
            source=source,
            track_url=track_url,
            playlist_url=playlist_url,
            link_url=match[1],
        )

    # 3/4. Spotify / Apple Music — a playlist/album takes priority over a single
    # track (a single-track album URL carries ``?i=`` and is excluded by
    # ``detect_playlist``).
    external_pl = links.detect_playlist(text)
    if external_pl:
        return ClassifiedInput(
            InputKind.EXTERNAL_PLAYLIST, text, external_url=external_pl
        )

    external_url = links.detect(text)
    if external_url:
        return ClassifiedInput(
            InputKind.EXTERNAL_TRACK, text, external_url=external_url
        )

    # 5. Plain text — search it.
    return ClassifiedInput(InputKind.SEARCH, text)


def external_items_to_tracks(items: List[ExternalItem], source: str) -> List[Track]:
    """Turn a scraped Spotify/Apple collection into query-backed tracks.

    These entries have no direct download URL — only an ``"artist title"`` query
    resolved on ``source`` at download time — so each gets a synthetic ``id`` and
    an empty ``url`` with the query attached (see :class:`~app.models.Track`).
    """
    return [
        Track(
            id=f"q{i}",
            title=item.title,
            url="",
            uploader=item.artist or None,
            source=source,
            query=item.query,
        )
        for i, item in enumerate(items)
    ]
