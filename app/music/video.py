"""TikTok video download via yt-dlp.

The bot is audio-first, but a pasted TikTok link points at a *video* — extracting
audio would throw away the point of the clip. This module recognizes TikTok URLs
and downloads the original clip as a single MP4, handing it to the bot's
video-delivery path (``bot/delivery.deliver_video``) which sends it with
``send_video`` instead of the MP3 ``send_audio`` pipeline.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import List, Optional, Pattern

from ..models import VideoFile
from .sources.ytdlp import THUMBNAIL_TO_JPG, download_media

logger = logging.getLogger(__name__)

# Prefer a progressive MP4 (video+audio in one file); fall back to merging the
# best streams into MP4 if a site serves them split.
_VIDEO_FORMAT = "best[ext=mp4]/bestvideo*+bestaudio/best"

# TikTok link forms: the full web host, the mobile/share shorteners (vm./vt.),
# and the m.tiktok.com host. We only need to recognize the link to download it.
_TIKTOK_PATTERNS: List[Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"(https?://)?(www\.|m\.)?tiktok\.com/\S+",
        r"(https?://)?(vm|vt)\.tiktok\.com/\S+",
    )
]

# Non-video files yt-dlp may drop next to the clip (thumbnail, subtitles, etc.).
_NON_VIDEO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".json", ".vtt", ".srt", ".part"}


def detect_tiktok(text: str) -> Optional[str]:
    """Return the TikTok URL found in ``text`` (canonical link), or None."""
    for pattern in _TIKTOK_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


class VideoDownloader:
    """Downloads a TikTok clip as a single MP4 ready to send."""

    def __init__(self, cookiefile: Optional[str] = None) -> None:
        self._cookiefile = cookiefile or None

    async def download(self, url: str, workdir: str) -> VideoFile:
        return await asyncio.to_thread(self._download, url, workdir)

    # --- blocking implementation (run in a worker thread) ----------------

    def _download(self, url: str, workdir: str) -> VideoFile:
        info = download_media(
            url,
            workdir,
            cookiefile=self._cookiefile,
            format=_VIDEO_FORMAT,
            merge_output_format="mp4",
            postprocessors=[THUMBNAIL_TO_JPG],
        )
        return _to_video_file(workdir, info)


def _to_video_file(workdir: str, info: dict) -> VideoFile:
    path = _find_video(workdir)
    if not path:
        return VideoFile(path="")
    thumb = next(
        (str(p) for p in Path(workdir).glob("*.jpg")), None
    )
    duration = info.get("duration")
    return VideoFile(
        path=str(path),
        title=info.get("title") or path.stem,
        uploader=info.get("uploader") or info.get("uploader_id") or info.get("creator"),
        # TikTok reports an int already, but yt-dlp can hand back a float.
        duration=int(duration) if duration is not None else None,
        width=info.get("width"),
        height=info.get("height"),
        thumb_path=thumb,
    )


def _find_video(workdir: str) -> Optional[Path]:
    """The downloaded clip: the largest file that isn't a thumbnail/sidecar."""
    candidates = [
        p
        for p in Path(workdir).iterdir()
        if p.is_file() and p.suffix.lower() not in _NON_VIDEO_EXT
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)
