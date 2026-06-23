"""SoundCloud audio source."""
from __future__ import annotations

from .ytdlp_source import YtDlpSource, compile_patterns


class SoundCloudSource(YtDlpSource):
    name = "soundcloud"
    search_prefix = "scsearch"
    url_patterns = compile_patterns(
        r"(https?://)?(www\.|m\.|api\.)?soundcloud\.com/\S+",
        r"(https?://)?on\.soundcloud\.com/\S+",
    )
