"""Shared yt-dlp powered source: download logic + optional prefix search."""
from __future__ import annotations

import asyncio
import os
import re
from typing import List, Optional, Pattern

import yt_dlp

from ..metadata import fetch_image, finalize_download, finalize_with_metadata
from ..metadata_provider import enrich
from ..models import AudioFile, Meta, Track
from .base import AudioSource


def extract_audio(url: str, workdir: str, quality: str) -> dict:
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

    def __init__(self, audio_quality: str = "192") -> None:
        self._quality = audio_quality

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

    async def download(
        self, url: str, workdir: str, meta: Optional[Meta] = None
    ) -> AudioFile:
        return await asyncio.to_thread(self._download, url, workdir, meta)

    # --- blocking implementations (run in a worker thread) ---------------

    def _search(self, query: str, limit: int) -> List[Track]:
        opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
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

    def _entry_to_track(self, entry: dict) -> Optional[Track]:
        url = entry.get("url") or entry.get("webpage_url")
        video_id = entry.get("id")
        if not url and not video_id:
            return None
        return Track(
            id=str(video_id),
            title=entry.get("title") or "Untitled",
            url=url or "",
            uploader=entry.get("uploader") or entry.get("channel"),
            duration=entry.get("duration"),
            source=self.name,
        )

    def _download(
        self, url: str, workdir: str, meta: Optional[Meta] = None
    ) -> AudioFile:
        info = extract_audio(url, workdir, self._quality)
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


def compile_patterns(*patterns: str) -> List[Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]
