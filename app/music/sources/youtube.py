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

from ...models import Track
from .ytdlp import YtDlpSource, compile_patterns

# Bump the low-res thumbnail YouTube Music returns (w120-h120) to a usable size.
_THUMB_SIZE = re.compile(r"w\d+-h\d+")

# A video id appears as ?v=…, in a youtu.be/… short link, or a /shorts/… path.
_VIDEO_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/)([\w-]{6,})", re.IGNORECASE)
_LIST_ID = re.compile(r"[?&]list=([\w-]+)", re.IGNORECASE)
# Auto-generated lists we can't (or shouldn't) enumerate: radio/mixes are
# effectively infinite; Liked/Watch-Later need the owner's auth. Treat a link
# carrying one of these as a plain single track.
_VIRTUAL_LIST = ("RD", "LL", "WL", "LM")


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

    def track_url(self, text: str) -> Optional[str]:
        match = _VIDEO_ID.search(text)
        return f"https://music.youtube.com/watch?v={match.group(1)}" if match else None

    def playlist_url(self, text: str) -> Optional[str]:
        match = _LIST_ID.search(text)
        if not match:
            return None
        list_id = match.group(1)
        if list_id.startswith(_VIRTUAL_LIST):
            return None
        return f"https://www.youtube.com/playlist?list={list_id}"

    def _canonical_url(self, entry: dict) -> str:
        video_id = entry.get("id")
        if video_id:
            return f"https://music.youtube.com/watch?v={video_id}"
        return entry.get("url") or entry.get("webpage_url") or ""

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
