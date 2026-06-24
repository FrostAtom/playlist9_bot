"""HTTP API behind the web download page.

The browser front-end mirrors the bot's text handling for *audio*: a plain
search (YouTube Music / SoundCloud), a pasted YouTube/SoundCloud track or
playlist link, or a Spotify/Apple Music track / playlist / album link. Each is
classified exactly like ``bot.router.on_text`` and collapsed to a list of
:class:`~app.models.Track` the page renders; downloading one streams the tagged
MP3 straight back to the browser (no Telegram file_id round-trip needed).

TikTok / video is deliberately out of scope here — this surface is music only.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from typing import List, Optional
from urllib.parse import quote

from aiohttp import web

from ..infra.limiter import DownloadLimiter, RateLimiter
from ..config import Settings
from ..infra.metrics import metrics
from ..models import Track
from ..music import links
from ..music.resolver import InputKind, classify, external_items_to_tracks
from ..music.service import MusicService

logger = logging.getLogger(__name__)

# Human-readable source names, kept here so the web layer doesn't import the
# aiogram presentation layer (bot.formatting).
_SOURCE_NAMES = {"youtube": "YouTube Music", "soundcloud": "SoundCloud"}


def source_name(source: str) -> str:
    return _SOURCE_NAMES.get(source, source.title())


def _track_to_dict(track: Track) -> dict:
    return {
        "id": track.id,
        "title": track.title,
        "uploader": track.uploader,
        "duration": track.duration,
        "album": track.album,
        "cover_url": track.cover_url,
        "source": track.source,
        "url": track.url,
        "query": track.query,
    }


def _client_id(request: web.Request) -> int:
    """A stable-ish per-client integer for the rate / concurrency limiters.

    There are no accounts here, so we key off the peer address (honouring a
    reverse proxy's ``X-Forwarded-For`` if present). Good enough to stop one
    client from monopolising the download slots; not a security control.
    """
    fwd = request.headers.get("X-Forwarded-For", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.remote or "anon")
    return hash(ip) & 0x7FFFFFFF


# ───────────────────────── search / classify ─────────────────────────

async def search(request: web.Request) -> web.Response:
    """Classify the query like the bot does and return the resulting tracks.

    Response shape::

        {"kind": "search"|"playlist", "source": str, "title": str|null,
         "tracks": [ {...} ]}
    """
    service: MusicService = request.app["service"]
    settings: Settings = request.app["settings"]
    text = (request.query.get("q") or "").strip()
    if not text:
        return web.json_response({"kind": "search", "source": "", "tracks": []})

    requested = request.query.get("source") or ""
    source = requested if requested in service.searchable_sources() else service.default_source()

    info = classify(service, text)
    kind = info.kind

    # TikTok is video — out of scope for this audio-only surface.
    if kind is InputKind.TIKTOK:
        return web.json_response(
            {"error": "TikTok videos aren't supported here — this page is audio only."},
            status=415,
        )

    # A YouTube/SoundCloud single track (an ambiguous watch?v=…&list=… link is
    # treated as the track itself, same as the bot's web parity).
    if kind in (InputKind.LINK_TRACK, InputKind.LINK_AMBIGUOUS):
        target = info.track_url or info.link_url or text
        track = await _resolve_single(service, target, info.source or source)
        return web.json_response(
            {"kind": "search", "source": info.source, "title": None, "tracks": [track]}
        )

    # A YouTube/SoundCloud playlist or set.
    if kind is InputKind.LINK_PLAYLIST:
        return await _playlist_response(service, settings, info.source, info.playlist_url)

    # A Spotify / Apple Music playlist or album.
    if kind is InputKind.EXTERNAL_PLAYLIST:
        return await _external_playlist_response(service, settings, text)

    # A Spotify / Apple Music single track → resolve to a query, then search.
    if kind is InputKind.EXTERNAL_TRACK:
        external = await asyncio.to_thread(links.resolve, text)
        if external is None:
            return web.json_response({"error": "Couldn't read that link."}, status=502)
        tracks = await _safe_search(service, external.query, settings.max_results, source)
        return web.json_response(
            {"kind": "search", "source": source, "title": external.provider, "tracks": tracks}
        )

    # Plain text search.
    metrics.incr("searches")
    tracks = await _safe_search(service, text, settings.max_results, source)
    return web.json_response(
        {"kind": "search", "source": source, "title": None, "tracks": tracks}
    )


async def _safe_search(
    service: MusicService, query: str, limit: int, source: str
) -> List[dict]:
    try:
        tracks = await service.search(query, limit, source)
    except Exception:  # noqa: BLE001 - surfaced to the client as an empty list
        logger.exception("Web search failed")
        return []
    return [_track_to_dict(t) for t in tracks]


async def _resolve_single(service: MusicService, url: str, source: str) -> dict:
    """A pasted single-track link → a Track dict (raw URL fallback if unknown)."""
    try:
        track = await service.resolve_track(url)
    except Exception:  # noqa: BLE001
        logger.exception("resolve_track failed")
        track = None
    if track is not None:
        return _track_to_dict(track)
    # No metadata available — mark it raw so /api/download fetches by URL string.
    return {
        "id": url,
        "title": "Download from link",
        "uploader": None,
        "duration": None,
        "album": None,
        "cover_url": None,
        "source": source,
        "url": url,
        "query": None,
        "raw": True,
    }


async def _playlist_response(
    service: MusicService, settings: Settings, source: str, url: str
) -> web.Response:
    try:
        tracks, title = await service.playlist(url, settings.playlist_limit, source)
    except Exception:  # noqa: BLE001
        logger.exception("Web playlist load failed")
        return web.json_response({"error": "Couldn't load that playlist."}, status=502)
    metrics.incr("playlists")
    return web.json_response(
        {
            "kind": "playlist",
            "source": source,
            "title": title or "Playlist",
            "tracks": [_track_to_dict(t) for t in tracks],
        }
    )


async def _external_playlist_response(
    service: MusicService, settings: Settings, text: str
) -> web.Response:
    playlist = await asyncio.to_thread(links.resolve_playlist, text, settings.playlist_limit)
    if playlist is None or not playlist.items:
        return web.json_response({"error": "Couldn't read that playlist."}, status=502)
    metrics.incr("playlists")
    source = service.default_source()
    tracks = external_items_to_tracks(playlist.items, source)
    return web.json_response(
        {
            "kind": "playlist",
            "source": source,
            "title": playlist.name,
            "tracks": [_track_to_dict(t) for t in tracks],
        }
    )


# ───────────────────────── download / stream ─────────────────────────

async def download(request: web.Request) -> web.Response:
    """Download one track and stream the tagged MP3 back as an attachment."""
    service: MusicService = request.app["service"]
    settings: Settings = request.app["settings"]
    limiter: DownloadLimiter = request.app["limiter"]
    rate: RateLimiter = request.app["rate"]

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "Invalid request."}, status=400)

    user = _client_id(request)
    if not rate.allow(user):
        metrics.incr("rate_limited")
        return web.json_response(
            {"error": f"Rate limit reached — try again in {rate.retry_after(user)}s."},
            status=429,
        )

    # A query-only item (Spotify/Apple pick) or a raw link downloads by string;
    # a full search result carries its tags through as a Track.
    raw = bool(body.get("raw"))
    query = (body.get("query") or "").strip()
    url = (body.get("url") or "").strip()

    if raw and url:
        ref = url
    elif not url and query:
        # Resolve the query to a concrete track on its source first.
        source = body.get("source") or service.default_source()
        matches = await _safe_search_tracks(service, query, 1, source)
        if not matches:
            return web.json_response({"error": "No match found."}, status=404)
        ref = matches[0]
    elif url:
        ref = Track(
            id=str(body.get("id") or url),
            title=body.get("title") or "",
            url=url,
            uploader=body.get("uploader") or None,
            duration=body.get("duration"),
            album=body.get("album") or None,
            cover_url=body.get("cover_url") or None,
            source=body.get("source") or "",
        )
    else:
        return web.json_response({"error": "Nothing to download."}, status=400)

    try:
        async with limiter.slot(user):
            with tempfile.TemporaryDirectory() as workdir:
                audio = await service.download(ref, workdir)
                if not audio.exists:
                    metrics.incr("downloads_failed")
                    return web.json_response({"error": "No audio found."}, status=404)
                if audio.size > settings.max_file_size:
                    metrics.incr("downloads_failed")
                    return web.json_response(
                        {"error": "File is larger than the size limit."}, status=413
                    )
                data = await asyncio.to_thread(_read_bytes, audio.path)
                filename = audio.filename
    except Exception:  # noqa: BLE001
        metrics.incr("downloads_failed")
        logger.exception("Web download failed")
        return web.json_response({"error": "Download failed."}, status=502)

    metrics.incr("downloads_ok")
    return web.Response(
        body=data,
        content_type="audio/mpeg",
        headers={"Content-Disposition": _attachment(filename)},
    )


async def _safe_search_tracks(
    service: MusicService, query: str, limit: int, source: str
) -> List[Track]:
    try:
        return await service.search(query, limit, source)
    except Exception:  # noqa: BLE001
        logger.exception("Web pick search failed")
        return []


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _attachment(filename: str) -> str:
    """RFC 5987 Content-Disposition that survives non-ASCII track titles."""
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "track.mp3"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"
