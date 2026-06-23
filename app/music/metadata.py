"""Clean up downloaded audio: sane filenames and embedded ID3 tags + cover.

The raw titles coming from YouTube et al. are full of junk like
``(Official Music Video)``, ``[HD]``, ``Lyrics``... This module strips that
noise, derives a clean ``Artist - Title.mp3`` filename, and embeds clean
title/artist/album tags plus the cover art into the MP3 with mutagen.

yt-dlp writes the thumbnail (converted to JPEG by FFmpegThumbnailsConvertor) and
basic tags during download; here we make the result clean and guarantee the
cover is embedded even when yt-dlp's own embedder would have skipped it.
"""
from __future__ import annotations

import io
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1
from mutagen.mp3 import MP3
from PIL import Image

from ..models import AudioFile

logger = logging.getLogger(__name__)

# Parenthetical/bracketed segments that are noise rather than part of a title.
_JUNK = re.compile(
    r"""\s*[\(\[\{]\s*[^()\[\]{}]*\b(
        official|video|audio|lyrics?|visuali[sz]er|mv|m/v|hd|hq|4k|8k|
        full\s*album|colou?r\s*coded|explicit|clean|remaster(ed)?|
        free\s*download|out\s*now|music\s*video|clip|teaser|trailer|
        live\s*performance|performance\s*video
    )\b[^()\[\]{}]*[\)\]\}]""",
    re.IGNORECASE | re.VERBOSE,
)

# Bare (non-bracketed) junk suffixes, e.g. "... GANGNAM STYLE M/V".
_TRAILING_JUNK = re.compile(
    r"""\s*[-–—|]?\s*\b(
        official\s*(music\s*)?(video|audio)|lyric\s*video|m/?v|mv|hd|hq|4k|8k|
        visuali[sz]er|audio|video
    )\b\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# Bare junk suffixes on an artist/channel name, e.g. "Queen Official".
_ARTIST_JUNK = re.compile(
    r"\s*-\s*Topic$|\bVEVO\b|\s+Official(\s+Music)?$|\s+Music$",
    re.IGNORECASE,
)

# Characters not allowed in file names on common filesystems.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Image extension -> MIME type, in order of preference for an MP3 cover.
_COVER_TYPES = ((".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".png", "image/png"))


def clean_title(title: str) -> str:
    title = _JUNK.sub("", title or "")
    # Strip bare trailing junk repeatedly (e.g. "... HD M/V").
    while True:
        stripped = _TRAILING_JUNK.sub("", title)
        if stripped == title:
            break
        title = stripped
    title = re.sub(r"\s{2,}", " ", title).strip(" -–—|\t")
    return title


def clean_artist(name: str) -> str:
    previous = None
    name = name or ""
    while name != previous:
        previous = name
        name = _ARTIST_JUNK.sub("", name).strip()
    return name


def sanitize_filename(name: str) -> str:
    name = _ILLEGAL.sub("", name or "")
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:120] or "audio"


def build_filename(artist: str, title: str) -> str:
    if artist and artist.lower() not in title.lower():
        base = f"{artist} - {title}"
    else:
        base = title or artist or "audio"
    return sanitize_filename(base) + ".mp3"


def finalize_download(workdir: str, info: dict) -> AudioFile:
    """Best-effort finalize for yt-dlp-native sources (YouTube, SoundCloud).

    Derives clean tags from the (messy) yt-dlp info and embeds the downloaded
    thumbnail as cover.
    """
    artist = clean_artist(
        info.get("artist") or info.get("uploader") or info.get("channel") or ""
    )
    title = clean_title(info.get("track") or info.get("title") or "")
    album = (info.get("album") or "").strip()
    return _finalize(workdir, title, artist, album, info.get("duration"), None)


def finalize_with_metadata(
    workdir: str,
    *,
    title: str,
    artist: str,
    album: Optional[str],
    duration: Optional[int],
    cover_bytes: Optional[bytes],
) -> AudioFile:
    """Finalize using authoritative metadata (e.g. from YouTube Music).

    The title/artist are already clean, so we only sanitize the filename and
    embed the provided tags + cover image.
    """
    return _finalize(workdir, title, artist, album or "", duration, cover_bytes)


def _finalize(
    workdir: str,
    title: str,
    artist: str,
    album: str,
    duration: Optional[int],
    cover_bytes: Optional[bytes],
) -> AudioFile:
    mp3 = next(Path(workdir).glob("*.mp3"), None)
    if not mp3:
        return AudioFile(path="")

    if not title:
        title = mp3.stem

    target = mp3.with_name(build_filename(artist, title))
    if target != mp3 and not target.exists():
        try:
            mp3 = mp3.rename(target)
        except OSError:
            pass

    # Resolve cover once: explicit bytes (search/MusicBrainz) else the
    # thumbnail yt-dlp downloaded next to the audio.
    cover = cover_bytes or _read_cover(Path(workdir))
    _embed_tags(mp3, title, artist, album, cover)
    thumb_path = _make_thumb(cover, Path(workdir)) if cover else None

    return AudioFile(
        path=str(mp3),
        title=title or None,
        uploader=artist or None,
        # Some sources (SoundCloud via yt-dlp) report a float; Telegram needs int.
        duration=int(duration) if duration is not None else None,
        thumb_path=thumb_path,
    )


def _read_cover(workdir: Path) -> Optional[bytes]:
    for ext, _mime in _COVER_TYPES:
        image = next(workdir.glob(f"*{ext}"), None)
        if image:
            try:
                return image.read_bytes()
            except OSError:
                continue
    return None


def _embed_tags(
    path: Path, title: str, artist: str, album: str, cover: Optional[bytes]
) -> None:
    try:
        audio = MP3(str(path), ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags

        if title:
            tags.setall("TIT2", [TIT2(encoding=3, text=title)])
        if artist:
            tags.setall("TPE1", [TPE1(encoding=3, text=artist)])
        if album:
            tags.setall("TALB", [TALB(encoding=3, text=album)])

        if cover:
            tags.delall("APIC")
            tags.add(
                APIC(
                    encoding=3,
                    mime=_sniff_mime(cover),
                    type=3,
                    desc="Cover",
                    data=cover,
                )
            )

        audio.save()
    except Exception:  # noqa: BLE001 - tagging must never break a download
        logger.warning("Failed to embed tags for %s", path, exc_info=True)


def _make_thumb(cover: bytes, workdir: Path) -> Optional[str]:
    """Build a Telegram-friendly thumbnail (JPEG, <=320px) from cover bytes."""
    try:
        image = Image.open(io.BytesIO(cover)).convert("RGB")
        image.thumbnail((320, 320))
        path = workdir / "thumb.jpg"
        image.save(path, "JPEG", quality=85)
        return str(path)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to build thumbnail", exc_info=True)
        return None


def _sniff_mime(data: bytes) -> str:
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


def fetch_image(url: Optional[str]) -> Optional[bytes]:
    """Download cover art bytes from a URL (used for search-provided art)."""
    if not url:
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        # A missing cover (e.g. Cover Art Archive has no art for this release →
        # 404) is an expected miss, not an error: the track is still delivered,
        # just without embedded art. Only surface unexpected HTTP failures.
        if exc.code in (403, 404, 410):
            logger.debug("No cover art at %s (HTTP %s)", url, exc.code)
        else:
            logger.warning("Cover art fetch failed for %s (HTTP %s)", url, exc.code)
        return None
    except Exception:  # noqa: BLE001
        logger.warning("Failed to fetch cover art from %s", url, exc_info=True)
        return None
