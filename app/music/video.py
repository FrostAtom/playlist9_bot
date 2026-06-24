"""TikTok download via yt-dlp (clips) + a small scraper (photo posts).

A pasted TikTok link can point at three different things, and we handle each on
its own path:

* a **video** (``/@user/video/<id>``) — downloaded as a single MP4 via the shared
  yt-dlp primitive and sent with ``send_video``;
* a **photo post** (``/@user/photo/<id>``) — a slideshow of images yt-dlp can't
  download, so we fetch the post page and pull the image URLs out of its embedded
  JSON, sending them as a Telegram media-group album;
* a **profile/channel** (``/@user``) — not a single post, so it's rejected.

Share shorteners (``vt.``/``vm.``) are opaque, so we resolve the redirect first
(one HTTP request) and classify by the *final* URL — far more reliable than
asking yt-dlp to guess.
"""
from __future__ import annotations

import asyncio
import http.cookiejar
import json
import logging
import os
import re
import urllib.request
from pathlib import Path
from typing import List, Optional, Pattern, Union

from ..models import PhotoAlbum, VideoFile
from .metadata import make_thumb
from .sources.ytdlp import THUMBNAIL_TO_JPG, download_media

logger = logging.getLogger(__name__)

# Prefer a progressive MP4 (video+audio in one file); fall back to merging the
# best streams into MP4 if a site serves them split.
_VIDEO_FORMAT = "best[ext=mp4]/bestvideo*+bestaudio/best"

# Telegram media groups hold at most 10 items — the cap on a photo album.
_MAX_PHOTOS = 10

# Browser-ish headers so TikTok serves the real page rather than a redirect stub.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Referer": "https://www.tiktok.com/",
}

# TikTok link forms we accept as a downloadable *post* (a clip or a photo post)
# or a share shortener. A bare profile/channel is deliberately not matched here
# as a clip — it's classified later, after the redirect is resolved.
_TIKTOK_PATTERNS: List[Pattern[str]] = [
    # Canonical post on the web host: …/@user/video/<id> or …/@user/photo/<id>.
    re.compile(
        r"(https?://)?(?:www\.|m\.)?tiktok\.com/@[\w.\-]+/(?:video|photo)/\d+",
        re.IGNORECASE,
    ),
    # Share shorteners — opaque codes that redirect to a single post.
    re.compile(r"(https?://)?(?:vm\.|vt\.)tiktok\.com/\S+", re.IGNORECASE),
    re.compile(r"(https?://)?(?:www\.|m\.)?tiktok\.com/t/\S+", re.IGNORECASE),
]

# Classification of a *resolved* TikTok URL.
_PHOTO_RE = re.compile(r"/photo/\d+", re.IGNORECASE)
_VIDEO_RE = re.compile(r"/video/\d+", re.IGNORECASE)
_PROFILE_RE = re.compile(r"tiktok\.com/@[\w.\-]+/?(?:[?#]|$)", re.IGNORECASE)

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


def _classify(url: str) -> str:
    """Bucket a resolved TikTok URL: 'photo', 'video', 'profile' or 'unknown'."""
    if _PHOTO_RE.search(url):
        return "photo"
    if _VIDEO_RE.search(url):
        return "video"
    if _PROFILE_RE.search(url):
        return "profile"
    return "unknown"


class VideoDownloader:
    """Downloads a TikTok post — a clip (MP4) or a photo slideshow (images)."""

    def __init__(self, cookiefile: Optional[str] = None) -> None:
        self._cookiefile = cookiefile or None

    async def download(
        self, url: str, workdir: str
    ) -> Union[VideoFile, PhotoAlbum]:
        return await asyncio.to_thread(self._download, url, workdir)

    # --- blocking implementation (run in a worker thread) ----------------

    def _download(self, url: str, workdir: str) -> Union[VideoFile, PhotoAlbum]:
        # Resolve the share-shortener redirect first so we know whether this is a
        # clip, a photo post or a profile — the URL alone (vt./vm.) doesn't say.
        opener = _cookie_opener(self._cookiefile)
        final, html = url, None
        try:
            req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
            with opener.open(req, timeout=30) as resp:
                final = resp.geturl()
                # Only read the (large) page body when we actually need it — a
                # photo post is the one case we parse out of the HTML.
                if _PHOTO_RE.search(final):
                    html = resp.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            logger.warning(
                "TikTok URL resolve failed for %s; falling back to yt-dlp",
                url,
                exc_info=True,
            )

        kind = _classify(final)

        if kind == "photo":
            return self._download_photos(html, opener, workdir)

        if kind == "profile":
            # A creator page, not a single post — nothing to download.
            logger.info("TikTok link is a profile/channel, not a post: %s", final)
            return VideoFile(path="")

        # 'video' or 'unknown' (resolve failed) — let yt-dlp fetch the clip.
        info = download_media(
            final,
            workdir,
            cookiefile=self._cookiefile,
            format=_VIDEO_FORMAT,
            merge_output_format="mp4",
            postprocessors=[THUMBNAIL_TO_JPG],
        )
        return _to_video_file(workdir, info)

    def _download_photos(
        self, html: Optional[str], opener: urllib.request.OpenerDirector, workdir: str
    ) -> PhotoAlbum:
        """Pull image URLs out of a photo post's page JSON and download them."""
        if not html:
            return PhotoAlbum()
        parsed = _parse_photo_post(html)
        if parsed is None:
            logger.info("TikTok photo post had no extractable images")
            return PhotoAlbum()
        paths: List[str] = []
        for index, image_url in enumerate(parsed["images"][:_MAX_PHOTOS]):
            dest = Path(workdir) / f"photo_{index:02d}.jpg"
            if _download_image(opener, image_url, dest):
                paths.append(str(dest))
        return PhotoAlbum(
            paths=tuple(paths),
            title=parsed.get("title"),
            uploader=parsed.get("uploader"),
        )


# --- photo-post scraping --------------------------------------------------


def _cookie_opener(cookiefile: Optional[str]) -> urllib.request.OpenerDirector:
    """An HTTP opener carrying the yt-dlp cookies (same cookies.txt as downloads),
    so TikTok serves the authenticated page rather than a stub."""
    handlers: list = []
    if cookiefile and os.path.exists(cookiefile):
        jar = http.cookiejar.MozillaCookieJar()
        try:
            jar.load(cookiefile, ignore_discard=True, ignore_expires=True)
            handlers.append(urllib.request.HTTPCookieProcessor(jar))
        except OSError:
            logger.warning("Could not load cookies %s for TikTok", cookiefile, exc_info=True)
    return urllib.request.build_opener(*handlers)


def _parse_photo_post(html: str) -> Optional[dict]:
    """Extract image URLs + caption from a TikTok photo post page.

    The page embeds its data in a ``__UNIVERSAL_DATA_FOR_REHYDRATION__`` script;
    a photo post carries ``itemStruct.imagePost.images[].imageURL.urlList`` (the
    first URL of each list is the full-size image). Returns None if the page
    doesn't contain a photo post (e.g. TikTok served a bot stub)."""
    match = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        html,
        re.S,
    )
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    item = (
        data.get("__DEFAULT_SCOPE__", {})
        .get("webapp.video-detail", {})
        .get("itemInfo", {})
        .get("itemStruct", {})
    )
    raw_images = (item.get("imagePost") or {}).get("images") or []
    images: List[str] = []
    for image in raw_images:
        urls = (image.get("imageURL") or {}).get("urlList") or []
        if urls:
            images.append(urls[0])
    if not images:
        return None
    return {
        "images": images,
        "title": item.get("desc") or None,
        "uploader": (item.get("author") or {}).get("uniqueId") or None,
    }


def _download_image(
    opener: urllib.request.OpenerDirector, url: str, dest: Path
) -> bool:
    try:
        req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
        with opener.open(req, timeout=30) as resp:
            data = resp.read()
    except Exception:  # noqa: BLE001
        logger.warning("TikTok photo image download failed: %s", url, exc_info=True)
        return False
    if not data:
        return False
    try:
        dest.write_bytes(data)
    except OSError:
        return False
    return True


# --- video file assembly --------------------------------------------------


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
