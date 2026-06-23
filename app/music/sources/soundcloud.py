"""SoundCloud audio source."""
from __future__ import annotations

from typing import Optional

from .ytdlp import YtDlpSource, compile_patterns


class SoundCloudSource(YtDlpSource):
    name = "soundcloud"
    search_prefix = "scsearch"
    url_patterns = compile_patterns(
        r"(https?://)?(www\.|m\.|api\.)?soundcloud\.com/\S+",
        r"(https?://)?on\.soundcloud\.com/\S+",
    )

    def track_url(self, text: str) -> Optional[str]:
        # A "set" (…/sets/…) is a playlist; everything else is a single track.
        url = self.handles(text)
        if not url or "/sets/" in url:
            return None
        return url

    def playlist_url(self, text: str) -> Optional[str]:
        url = self.handles(text)
        return url if url and "/sets/" in url else None
