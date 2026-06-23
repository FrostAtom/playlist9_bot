"""Metadata enrichment via MusicBrainz + Cover Art Archive.

Used to fill gaps (album / cover art) when a source's own metadata is
incomplete — e.g. SoundCloud tracks. MusicBrainz was picked over TheAudioDB
(weak free tier) and Discogs (requires a token) because it has solid, free,
keyless coverage including Russian music.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_MB_URL = "https://musicbrainz.org/ws/2/recording"
_CAA_FRONT = "https://coverartarchive.org/release/{}/front-500"
_UA = {"User-Agent": "playlist9-bot/1.0 (+https://t.me)"}

# Small in-process cache to be gentle with MusicBrainz (≈1 req/s policy).
_cache: dict = {}


def enrich(
    artist: str, title: str, album: Optional[str], cover_url: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Return (album, cover_url), filling missing values from MusicBrainz."""
    if (album and cover_url) or not (artist and title):
        return album, cover_url

    key = f"{artist}\n{title}".lower()
    if key in _cache:
        rel_title, rel_id = _cache[key]
    else:
        rel_title, rel_id = _lookup(artist, title)
        _cache[key] = (rel_title, rel_id)

    if rel_title and not album:
        album = rel_title
    if rel_id and not cover_url:
        cover_url = _CAA_FRONT.format(rel_id)
    return album, cover_url


def _lookup(artist: str, title: str) -> Tuple[Optional[str], Optional[str]]:
    query = urllib.parse.quote(f'recording:"{title}" AND artist:"{artist}"')
    url = f"{_MB_URL}?query={query}&fmt=json&limit=1"
    try:
        request = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
    except Exception:  # noqa: BLE001
        logger.warning("MusicBrainz lookup failed", exc_info=True)
        return None, None

    recordings = data.get("recordings") or []
    if not recordings:
        return None, None
    releases = recordings[0].get("releases") or []
    if not releases:
        return None, None
    release = releases[0]
    return release.get("title"), release.get("id")
