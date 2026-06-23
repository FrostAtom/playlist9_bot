"""Resolve pasted Spotify / Apple Music track links into a search query.

These platforms are DRM-protected, so the audio can't be downloaded from them
directly. Instead we read the track's artist + title from the page's Open Graph
/ meta tags (keyless, no API access) and hand a ``"artist title"`` query to the
normal search pipeline, which finds and downloads the matching track from
YouTube Music with clean metadata.
"""
from __future__ import annotations

import html
import logging
import re
import urllib.request
from dataclasses import dataclass
from typing import Optional

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
