"""YouTube Music source.

Search goes through YouTube Music (ytmusicapi, unauthenticated) with the
``songs`` filter, so results are actual tracks — not random videos — and come
with clean metadata (artist, title, album, square album art). The audio itself
is downloaded from YouTube via yt-dlp; the YouTube Music metadata is embedded.
"""
from __future__ import annotations

import asyncio
import re
import threading
from typing import List, Optional

from ..models import Track
from .ytdlp_source import YtDlpSource, compile_patterns

# Bump the low-res thumbnail YouTube Music returns (w120-h120) to a usable size.
_THUMB_SIZE = re.compile(r"w\d+-h\d+")


class YouTubeMusicSource(YtDlpSource):
    name = "youtube"
    url_patterns = compile_patterns(
        r"(https?://)?(www\.|music\.)?(youtube\.com|youtu\.be)/\S+",
    )

    def __init__(
        self, audio_quality: str = "320", cookiefile: Optional[str] = None
    ) -> None:
        super().__init__(audio_quality, cookiefile)
        self._yt = None
        self._lock = threading.Lock()

    def _client(self):
        if self._yt is None:
            with self._lock:
                if self._yt is None:
                    from ytmusicapi import YTMusic

                    self._yt = YTMusic()
        return self._yt

    async def search(self, query: str, limit: int) -> List[Track]:
        return await asyncio.to_thread(self._search, query, limit)

    def _search(self, query: str, limit: int) -> List[Track]:
        results = self._client().search(query, filter="songs", limit=limit)
        tracks: List[Track] = []
        for item in results[:limit]:
            video_id = item.get("videoId")
            if not video_id:
                continue
            thumbs = item.get("thumbnails") or []
            cover = thumbs[-1]["url"] if thumbs else None
            if cover:
                cover = _THUMB_SIZE.sub("w544-h544", cover)
            album = item.get("album") or {}
            tracks.append(
                Track(
                    id=video_id,
                    title=item.get("title") or "Untitled",
                    url=f"https://music.youtube.com/watch?v={video_id}",
                    uploader=", ".join(a["name"] for a in item.get("artists") or []),
                    duration=item.get("duration_seconds"),
                    album=album.get("name"),
                    cover_url=cover,
                    source=self.name,
                )
            )
        return tracks
