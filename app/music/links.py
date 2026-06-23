"""Resolve pasted Spotify / Apple Music track links into a search query.

These platforms are DRM-protected, so the audio can't be downloaded from them
directly. Instead we read the track's artist + title from the page's Open Graph
/ meta tags (keyless, no API access) and hand a ``"artist title"`` query to the
normal search pipeline, which finds and downloads the matching track from
YouTube Music with clean metadata.
"""
from __future__ import annotations

import html
import json
import logging
import re
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# English meta tags are easier to parse ("… Song · 1987", "… by a-ha on Apple
# Music"), so we ask for an English page regardless of the bot's locale.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Language": "en-US,en;q=0.9",
}

# Track-link patterns. Each match strips tracking query params by capturing only
# up to the id, giving a clean canonical URL to fetch.
_SPOTIFY_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?track/[A-Za-z0-9]+",
    re.IGNORECASE,
)
_APPLE_RE = re.compile(
    r"https?://music\.apple\.com/[a-z]{2}/"
    r"(?:album/[^?\s]+\?i=\d+|song/[^?\s/]+/\d+)",
    re.IGNORECASE,
)

# Collection-link patterns (playlists *and* albums). Like single tracks these
# are DRM-protected, so we read the track list (names + artists) and search each
# on YouTube Music. The captured kind ("playlist"/"album") selects the right
# Spotify embed endpoint; both kinds share the same page structure.
_SPOTIFY_COLLECTION_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?(playlist|album)/([A-Za-z0-9]+)",
    re.IGNORECASE,
)
# An Apple album track is "album/<name>/<id>?i=<trackid>" (a single track, caught
# by _APPLE_RE); the same URL *without* ?i= is the whole album. Playlists are
# "playlist/<name>/pl.<id>".
_APPLE_COLLECTION_RE = re.compile(
    r"https?://music\.apple\.com/[a-z]{2}/(?:playlist|album)/[^?\s/]+/"
    r"(?:pl\.[A-Za-z0-9.-]+|\d+)",
    re.IGNORECASE,
)
_APPLE_SINGLE_TRACK = re.compile(r"[?&]i=\d+")


@dataclass(frozen=True)
class ExternalTrack:
    """What we recovered from an external link, ready to search for."""

    query: str  # "artist title", fed to the search pipeline
    title: str
    artist: str
    provider: str  # human-readable, e.g. "Spotify"


def detect(text: str) -> Optional[str]:
    """Return a canonical track URL if ``text`` contains a supported link."""
    for pattern in (_SPOTIFY_RE, _APPLE_RE):
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def resolve(text: str) -> Optional[ExternalTrack]:
    """Fetch the link and extract its track. Blocking — call via ``to_thread``."""
    url = detect(text)
    if not url:
        return None
    page = _fetch(url)
    if not page:
        return None
    if "open.spotify.com" in url.lower():
        return _parse_spotify(page)
    return _parse_apple(page)


def _parse_spotify(page: str) -> Optional[ExternalTrack]:
    # og:title -> track name; og:description -> "Artist · Album · Song · 1987".
    title = _meta(page, "og:title")
    if not title:
        return None
    description = _meta(page, "og:description") or ""
    artist = description.split("·")[0].strip() if description else ""
    return ExternalTrack(
        query=f"{artist} {title}".strip(),
        title=title,
        artist=artist,
        provider="Spotify",
    )


def _parse_apple(page: str) -> Optional[ExternalTrack]:
    # apple:title -> clean track name; og:title -> "Title by Artist on Apple Music".
    title = _meta(page, "apple:title")
    og_title = _meta(page, "og:title") or ""
    match = re.search(r"\bby (.+?) on Apple", og_title)
    artist = match.group(1).strip() if match else ""
    if not title:
        title = re.sub(r"\s+by .+? on Apple.*$", "", og_title).strip() or og_title
    if not title:
        return None
    return ExternalTrack(
        query=f"{artist} {title}".strip(),
        title=title,
        artist=artist,
        provider="Apple Music",
    )


def _meta(page: str, key: str) -> Optional[str]:
    """Read the ``content`` of a ``<meta>`` tag by its property/name, order-agnostic."""
    for tag in re.finditer(r"<meta\b[^>]*>", page, re.IGNORECASE):
        text = tag.group(0)
        if re.search(rf'(?:property|name)\s*=\s*"{re.escape(key)}"', text, re.IGNORECASE):
            content = re.search(r'content\s*=\s*"([^"]*)"', text, re.IGNORECASE)
            if content:
                return html.unescape(content.group(1)).strip()
    return None


@dataclass(frozen=True)
class ExternalItem:
    """One playlist entry, as a search query plus its display parts."""

    query: str   # "artist title", fed to the search pipeline
    title: str
    artist: str


@dataclass(frozen=True)
class ExternalPlaylist:
    name: str
    provider: str           # human-readable, e.g. "Spotify"
    items: List[ExternalItem]


def detect_playlist(text: str) -> Optional[str]:
    """Return a canonical playlist/album URL if ``text`` contains a supported one."""
    spotify = _SPOTIFY_COLLECTION_RE.search(text)
    if spotify:
        return spotify.group(0)
    apple = _APPLE_COLLECTION_RE.search(text)
    # An Apple "album/…?i=…" URL is a single track, not the whole album.
    if apple and not _APPLE_SINGLE_TRACK.search(text):
        return apple.group(0)
    return None


def resolve_playlist(text: str, limit: int) -> Optional[ExternalPlaylist]:
    """Scrape a playlist/album link into up to ``limit`` searchable items.

    Blocking (network + parsing) — call via ``to_thread``."""
    spotify = _SPOTIFY_COLLECTION_RE.search(text)
    if spotify:
        kind, spotify_id = spotify.group(1).lower(), spotify.group(2)
        name, items = _spotify_collection(kind, spotify_id, limit)
        return (
            ExternalPlaylist(name or f"Spotify {kind}", "Spotify", items)
            if items
            else None
        )
    apple = _APPLE_COLLECTION_RE.search(text)
    if apple and not _APPLE_SINGLE_TRACK.search(text):
        name, items = _apple_collection(apple.group(0), limit)
        return (
            ExternalPlaylist(name or "Apple Music", "Apple Music", items)
            if items
            else None
        )
    return None


def _spotify_collection(
    kind: str, spotify_id: str, limit: int
) -> Tuple[Optional[str], List[ExternalItem]]:
    # The embed page ships a __NEXT_DATA__ JSON blob with the full track list
    # (title + subtitle=artist) — far richer than the main page's <head> meta,
    # which caps at ~30 bare track URLs. Playlists and albums share this shape.
    page = _fetch(f"https://open.spotify.com/embed/{kind}/{spotify_id}")
    if not page:
        return None, []
    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.DOTALL
    )
    if not match:
        return None, []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.warning("Spotify playlist JSON parse failed", exc_info=True)
        return None, []
    entity = _find_dict_with_key(data, "trackList")
    if not entity:
        return None, []
    items: List[ExternalItem] = []
    for entry in (entity.get("trackList") or [])[:limit]:
        title = (entry.get("title") or "").strip()
        artist = (entry.get("subtitle") or "").strip()
        if not title:
            continue
        items.append(
            ExternalItem(query=f"{artist} {title}".strip(), title=title, artist=artist)
        )
    name = entity.get("name") or entity.get("title")
    return name, items


def _apple_collection(url: str, limit: int) -> Tuple[Optional[str], List[ExternalItem]]:
    # JSON-LD describes both kinds, but a MusicPlaylist lists its songs under
    # "track" while a MusicAlbum uses "tracks" — accept either.
    page = _fetch(url)
    if not page:
        return None, []
    match = re.search(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        page,
        re.DOTALL,
    )
    if not match:
        return None, []
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.warning("Apple collection JSON-LD parse failed", exc_info=True)
        return None, []
    entries = data.get("track") or data.get("tracks") or []
    names = [t.get("name", "").strip() for t in entries if t.get("name")]
    # JSON-LD omits per-track artists; recover them from the page's server data
    # and pair by index — but only when the counts line up exactly, so a layout
    # change can never silently mis-attribute artists. Failing that, fall back to
    # the collection's own artist (albums are single-artist) for a usable query.
    artists = _apple_artists(page)
    aligned = artists if len(artists) == len(names) else []
    album_artist = _apple_album_artist(data)
    items: List[ExternalItem] = []
    for i, title in enumerate(names[:limit]):
        artist = aligned[i] if aligned else album_artist
        items.append(
            ExternalItem(query=f"{artist} {title}".strip(), title=title, artist=artist)
        )
    return data.get("name"), items


def _apple_album_artist(data: dict) -> str:
    by = data.get("byArtist")
    if isinstance(by, list):
        by = by[0] if by else None
    if isinstance(by, dict):
        return (by.get("name") or "").strip()
    return ""


def _apple_artists(page: str) -> List[str]:
    match = re.search(
        r'<script\b[^>]*\bid="serialized-server-data"[^>]*>(.*?)</script>',
        page,
        re.DOTALL,
    )
    if not match:
        return []
    try:
        data = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return []
    artists: List[str] = []

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            name = obj.get("artistName")
            if isinstance(name, str) and name:
                artists.append(name)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(data)
    return artists


def _find_dict_with_key(obj: object, key: str) -> Optional[dict]:
    """Depth-first search for the first dict carrying ``key`` (a list value)."""
    if isinstance(obj, dict):
        if isinstance(obj.get(key), list):
            return obj
        for value in obj.values():
            found = _find_dict_with_key(value, key)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_dict_with_key(value, key)
            if found:
                return found
    return None


def _fetch(url: str) -> Optional[str]:
    try:
        request = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(request, timeout=15) as response:
            # Open Graph tags live in <head>; cap the read so a huge page body
            # can't stall us.
            raw = response.read(1_500_000)
            charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except Exception:  # noqa: BLE001
        logger.warning("Failed to fetch external link %s", url, exc_info=True)
        return None
