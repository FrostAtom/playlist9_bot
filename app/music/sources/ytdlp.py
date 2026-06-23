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
    r"does not exist|age|sign in to confirm|members[- ]only|geo)",
    re.IGNORECASE,
)


def extract_audio(
    url: str, workdir: str, quality: str, cookiefile: Optional[str] = None
) -> dict:
    """Download ``url`` as an MP3 into ``workdir``; return the yt-dlp info dict.

    A ``ytsearchN:`` / ``scsearchN:`` query also works as ``url`` when a source
    needs to resolve audio from a search rather than a direct link.
    """
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(workdir, "%(title).150s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "writethumbnail": True,
        # Let yt-dlp ride out transient network hiccups before raising.
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "socket_timeout": 30,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": quality,
            },
            {"key": "FFmpegMetadata", "add_metadata": True},
            # Normalize the thumbnail to JPEG (YouTube often serves webp, which
            # can't be embedded as an MP3 cover). Embedding is done in
            # metadata.finalize_* with mutagen for reliability.
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
        ],
    }
    if cookiefile and os.path.exists(cookiefile):
        opts["cookiefile"] = cookiefile
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


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

    # --- blocking implementations (run in a worker thread) ---------------

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
        info = self._extract_with_retry(url, workdir)
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

    def _extract_with_retry(
        self, url: str, workdir: str, attempts: int = 3
    ) -> dict:
        """Run :func:`extract_audio`, retrying transient failures with backoff.

        The work dir is wiped between attempts so a half-written file from a
        failed try can't be picked up as the result.
        """
        delay = 2.0
        for attempt in range(1, attempts + 1):
            try:
                return extract_audio(url, workdir, self._quality, self._cookiefile)
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


def _clear_dir(workdir: str) -> None:
    for path in Path(workdir).iterdir():
        try:
            path.unlink()
        except OSError:
            pass


def compile_patterns(*patterns: str) -> List[Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]
