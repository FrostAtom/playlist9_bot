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
import os
import re
from pathlib import Path
from typing import List, Optional, Pattern

import yt_dlp

from ..models import VideoFile
from .metadata import make_thumb
from .sources.ytdlp import THUMBNAIL_TO_JPG, download_media

logger = logging.getLogger(__name__)

# Prefer a progressive MP4 (video+audio in one file); fall back to merging the
# best streams into MP4 if a site serves them split.
_VIDEO_FORMAT = "best[ext=mp4]/bestvideo*+bestaudio/best"

# TikTok link forms we can actually download as a clip. We deliberately match
# only *video* URLs and the share shorteners — never a bare profile/channel
# (`tiktok.com/@user`) or a photo post, which have no MP4 and would otherwise
# make yt-dlp churn through retries before failing with a confusing error. Each
# pattern carries its own host so the *whole* URL (including the `vt.`/`vm.`
# prefix a shortener redirect depends on) is captured.
_TIKTOK_PATTERNS: List[Pattern[str]] = [
    # Canonical clip on the web host: …/@user/video/<id>.
    re.compile(
        r"(https?://)?(?:www\.|m\.)?tiktok\.com/@[\w.\-]+/video/\d+", re.IGNORECASE
    ),
    # Share shorteners — opaque codes that redirect to a single clip.
    re.compile(r"(https?://)?(?:vm\.|vt\.)tiktok\.com/\S+", re.IGNORECASE),
    re.compile(r"(https?://)?(?:www\.|m\.)?tiktok\.com/t/\S+", re.IGNORECASE),
]

# Non-video files yt-dlp may drop next to the clip (thumbnail, subtitles, etc.).
_NON_VIDEO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".json", ".vtt", ".srt", ".part"}


def detect_tiktok(text: str) -> Optional[str]:
    """Return the TikTok URL found in ``text`` (with a scheme), or None."""
    for pattern in _TIKTOK_PATTERNS:
        match = pattern.search(text)
        if match:
            url = match.group(0)
            # yt-dlp needs an absolute URL; the `vt.`/`vm.` share links are pure
            # redirects, so a scheme-less paste must still get one.
            return url if url.lower().startswith("http") else f"https://{url}"
    return None


class VideoDownloader:
    """Downloads a TikTok clip as a single MP4 ready to send."""

    def __init__(self, cookiefile: Optional[str] = None) -> None:
        self._cookiefile = cookiefile or None

    async def download(self, url: str, workdir: str) -> VideoFile:
        return await asyncio.to_thread(self._download, url, workdir)

    # --- blocking implementation (run in a worker thread) ----------------

    def _download(self, url: str, workdir: str) -> VideoFile:
        # Resolve the link first (no download). A share shortener (vt./vm.) is
        # opaque — it can redirect to a single clip *or* to a profile/channel.
        # A profile expands into a playlist of many clips; downloading that would
        # pull the creator's whole feed, so we reject anything that isn't a single
        # clip here and report it as "no video" rather than churning through
        # retries (the symptom users saw: a long hang ending in an error).
        probe = self._probe(url)
        if probe is not None and (
            probe.get("entries") is not None or probe.get("_type") == "playlist"
        ):
            logger.info("TikTok link is a profile/playlist, not a clip: %s", url)
            return VideoFile(path="")

        target = (probe or {}).get("webpage_url") or url
        info = download_media(
            target,
            workdir,
            cookiefile=self._cookiefile,
            format=_VIDEO_FORMAT,
            merge_output_format="mp4",
            postprocessors=[THUMBNAIL_TO_JPG],
        )
        return _to_video_file(workdir, info)

    def _probe(self, url: str) -> Optional[dict]:
        """Metadata-only resolve to tell a single clip from a profile/playlist.

        Uses a flat extract (no per-clip network calls) and caps the playlist at
        one entry, so a creator page is recognized as a playlist cheaply. Returns
        None if extraction fails — the caller then falls back to a normal download
        attempt, so a flaky probe never blocks a genuine clip."""
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "playlistend": 1,
            "extractor_retries": 1,
            "socket_timeout": 30,
        }
        if self._cookiefile and os.path.exists(self._cookiefile):
            opts["cookiefile"] = self._cookiefile
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError:
            logger.warning("TikTok probe failed for %s", url, exc_info=True)
            return None


def _to_video_file(workdir: str, info: dict) -> VideoFile:
    path = _find_video(workdir)
    if not path:
        return VideoFile(path="")
    # Telegram rejects an oversized poster, and TikTok's is full-resolution — so
    # downscale it to a compliant JPEG instead of handing over the raw image.
    thumb = _build_thumb(Path(workdir))
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


def _build_thumb(workdir: Path) -> Optional[str]:
    """Downscale the downloaded poster to a Telegram-friendly thumbnail."""
    raw = next(workdir.glob("*.jpg"), None)
    if raw is None:
        return None
    try:
        data = raw.read_bytes()
    except OSError:
        return None
    return make_thumb(data, workdir)


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
