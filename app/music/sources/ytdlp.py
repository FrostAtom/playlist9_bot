"""Shared yt-dlp powered source: download logic + optional prefix search."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import List, Optional, Pattern, Tuple

import yt_dlp

from ..metadata import fetch_image, finalize_download, finalize_with_metadata
from ..metadata_provider import enrich
from ...models import AudioFile, Meta, Track
from .base import AudioSource

logger = logging.getLogger(__name__)

# Download errors worth retrying are transient (network blips, 5xx, throttling);
# these substrings mark a *permanent* failure that retrying can't fix.
_PERMANENT_ERROR = re.compile(
    r"(unavailable|private|removed|deleted|copyright|not available|"
    r"does not exist|age|sign in to confirm|members[- ]only|geo|"
    # "Unsupported URL" is what yt-dlp raises for e.g. a TikTok *photo* post —
    # a different content type it can't download. Retrying never helps.
    r"unsupported url)",
    re.IGNORECASE,
)


# Normalize any downloaded thumbnail to JPEG (sites often serve webp, which can't
# be embedded as an MP3 cover and isn't a Telegram-friendly video thumb). Shared
# by audio and video downloads.
THUMBNAIL_TO_JPG = {"key": "FFmpegThumbnailsConvertor", "format": "jpg"}


def _base_opts(workdir: str, cookiefile: Optional[str]) -> dict:
    """yt-dlp options common to every download (audio and video)."""
    opts = {
        "outtmpl": os.path.join(workdir, "%(title).150s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # No carriage-return progress bars: this runs headless in a worker thread.
        "noprogress": True,
        "writethumbnail": True,
        # Let yt-dlp ride out transient network hiccups before raising.
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "socket_timeout": 30,
    }
    # A single cookies.txt (COOKIES_FILE) serves every site — yt-dlp picks the
    # cookies matching each request's domain (YouTube, SoundCloud, TikTok, …).
    if cookiefile and os.path.exists(cookiefile):
        opts["cookiefile"] = cookiefile
    return opts


def download_media(
    url: str,
    workdir: str,
    *,
    cookiefile: Optional[str] = None,
    attempts: int = 3,
    **extra_opts,
) -> dict:
    """Download ``url`` into ``workdir`` via yt-dlp; return the info dict.

    The single download primitive shared by the audio sources and the video
    downloader: it layers the per-call ``extra_opts`` (``format``,
    ``postprocessors``, ``merge_output_format``, …) onto the common base options,
    applies cookies, and retries transient failures with exponential backoff. The
    work dir is wiped between attempts so a half-written file from a failed try
    can't be picked up as the result. A ``ytsearchN:`` / ``scsearchN:`` query also
    works as ``url`` when resolving audio from a search rather than a direct link.
    """
    opts = _base_opts(workdir, cookiefile)
    opts.update(extra_opts)
    delay = 2.0
    for attempt in range(1, attempts + 1):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as exc:
            if attempt >= attempts or _PERMANENT_ERROR.search(str(exc)):
                raise
            logger.warning(
                "Download attempt %d/%d failed (%s); retrying in %.0fs",
                attempt,
                attempts,
                exc,
                delay,
            )
            _clear_dir(workdir)
            time.sleep(delay)
            delay *= 2
    # Unreachable: the loop either returns or raises.
    raise RuntimeError("retry loop exhausted")


def _audio_opts(quality: str) -> dict:
    """Per-call yt-dlp options for an MP3 download at ``quality`` kbps."""
    return {
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": quality,
            },
            {"key": "FFmpegMetadata", "add_metadata": True},
            # Embedding the cover is done in metadata.finalize_* with mutagen.
            THUMBNAIL_TO_JPG,
        ],
    }


class YtDlpSource(AudioSource):
    """Base class for sources backed by yt-dlp.

    Subclasses set ``name``, ``url_patterns`` and (optionally) ``search_prefix``
    (e.g. ``"scsearch"`` for SoundCloud). Sources that need a bespoke search
    (YouTube Music) override :meth:`search`; sources without search leave
    ``search_prefix`` as ``None`` and may set ``searchable = False``.
    """

    url_patterns: List[Pattern[str]] = []
    search_prefix: Optional[str] = None

    def __init__(
        self, audio_quality: str = "320", cookiefile: Optional[str] = None
    ) -> None:
        self._quality = audio_quality
        self._cookiefile = cookiefile or None

    def handles(self, text: str) -> Optional[str]:
        for pattern in self.url_patterns:
            match = pattern.search(text)
            if match:
                return match.group(0)
        return None

    async def search(self, query: str, limit: int) -> List[Track]:
        if not self.search_prefix:
            return []
        return await asyncio.to_thread(self._search, query, limit)

    async def list_playlist(
        self, url: str, limit: int
    ) -> Tuple[List[Track], Optional[str]]:
        return await asyncio.to_thread(self._list_playlist, url, limit)

    async def download(
        self, url: str, workdir: str, meta: Optional[Meta] = None
    ) -> AudioFile:
        return await asyncio.to_thread(self._download, url, workdir, meta)

    async def resolve_track(self, url: str) -> Optional[Track]:
        return await asyncio.to_thread(self._resolve_track, url)

    # --- blocking implementations (run in a worker thread) ---------------

    def _resolve_track(self, url: str) -> Optional[Track]:
        """Metadata-only extract (no download) to turn a URL into a Track."""
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }
        if self._cookiefile and os.path.exists(self._cookiefile):
            opts["cookiefile"] = self._cookiefile
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError:
            logger.warning("Could not resolve track URL %s", url, exc_info=True)
            return None
        if not info:
            return None
        # A link that's actually a playlist yields entries; take the first track.
        if info.get("entries"):
            entries = [e for e in info["entries"] if e]
            info = entries[0] if entries else {}
        video_id = info.get("id")
        if not video_id:
            return None
        duration = info.get("duration")
        return Track(
            id=str(video_id),
            title=info.get("track") or info.get("title") or "Untitled",
            url=info.get("webpage_url") or url,
            uploader=info.get("artist") or info.get("uploader") or info.get("channel"),
            duration=int(duration) if duration is not None else None,
            source=self.name,
        )

    def _search(self, query: str, limit: int) -> List[Track]:
        opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
        if self._cookiefile and os.path.exists(self._cookiefile):
            opts["cookiefile"] = self._cookiefile
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"{self.search_prefix}{limit}:{query}", download=False
            )
        tracks = []
        for entry in info.get("entries") or []:
            track = self._entry_to_track(entry) if entry else None
            if track:
                tracks.append(track)
        return tracks

    def _list_playlist(
        self, url: str, limit: int
    ) -> Tuple[List[Track], Optional[str]]:
        """Flat-extract a playlist's entries (no per-track network calls)."""
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "playlistend": limit,
        }
        if self._cookiefile and os.path.exists(self._cookiefile):
            opts["cookiefile"] = self._cookiefile
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        tracks: List[Track] = []
        for entry in (info.get("entries") or [])[:limit]:
            track = self._entry_to_track(entry) if entry else None
            if track:
                tracks.append(track)
        return tracks, info.get("title")

    def _canonical_url(self, entry: dict) -> str:
        """A downloadable URL for a flat-extracted entry.

        yt-dlp's flat entries sometimes carry only a bare id, so subclasses
        (e.g. YouTube) override this to build a full watch URL."""
        return entry.get("url") or entry.get("webpage_url") or ""

    def _entry_to_track(self, entry: dict) -> Optional[Track]:
        video_id = entry.get("id")
        url = self._canonical_url(entry)
        if not url and not video_id:
            return None
        return Track(
            id=str(video_id),
            title=entry.get("title") or "Untitled",
            url=url,
            uploader=entry.get("uploader") or entry.get("channel"),
            duration=entry.get("duration"),
            source=self.name,
        )

    def _download(
        self, url: str, workdir: str, meta: Optional[Meta] = None
    ) -> AudioFile:
        info = download_media(
            url, workdir, cookiefile=self._cookiefile, **_audio_opts(self._quality)
        )
        if meta:
            album, cover_url = meta.album, meta.cover_url
            if not album or not cover_url:
                album, cover_url = enrich(meta.artist, meta.title, album, cover_url)
            return finalize_with_metadata(
                workdir,
                title=meta.title,
                artist=meta.artist,
                album=album,
                duration=meta.duration,
                cover_bytes=fetch_image(cover_url),
            )
        return finalize_download(workdir, info)


def _clear_dir(workdir: str) -> None:
    for path in Path(workdir).iterdir():
        try:
            path.unlink()
        except OSError:
            pass


def compile_patterns(*patterns: str) -> List[Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]
