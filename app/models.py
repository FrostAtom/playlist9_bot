"""Domain models shared across the application."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass(frozen=True)
class Track:
    """A track found in search results."""

    id: str
    title: str
    url: str
    uploader: Optional[str] = None
    duration: Optional[int] = None
    album: Optional[str] = None
    cover_url: Optional[str] = None
    #: Identifier of the source this track came from (e.g. "youtube").
    source: str = ""
    #: Set only for playlist items that have no direct download URL (Spotify /
    #: Apple Music). When present, picking the track searches ``source`` for this
    #: query and downloads the top match instead of fetching ``url``.
    query: Optional[str] = None


@dataclass(frozen=True)
class Meta:
    """Authoritative metadata for a track, used to tag the downloaded file."""

    title: str
    artist: str
    album: Optional[str] = None
    cover_url: Optional[str] = None
    duration: Optional[int] = None


@dataclass(frozen=True)
class AudioFile:
    """A downloaded audio file ready to be sent."""

    path: str
    title: Optional[str] = None
    uploader: Optional[str] = None
    duration: Optional[int] = None
    thumb_path: Optional[str] = None

    @property
    def exists(self) -> bool:
        return bool(self.path) and os.path.exists(self.path)

    @property
    def size(self) -> int:
        return os.path.getsize(self.path) if self.exists else 0

    @property
    def filename(self) -> str:
        return os.path.basename(self.path)


@dataclass(frozen=True)
class PhotoAlbum:
    """Images downloaded from a TikTok photo (slideshow) post, ready to be sent
    as a Telegram media group. TikTok photo posts have no MP4, so they take a
    separate delivery path from :class:`VideoFile`."""

    paths: Tuple[str, ...] = field(default_factory=tuple)
    title: Optional[str] = None
    uploader: Optional[str] = None

    @property
    def existing(self) -> Tuple[str, ...]:
        """Only the image paths that were actually written to disk."""
        return tuple(p for p in self.paths if p and os.path.exists(p))

    @property
    def exists(self) -> bool:
        return bool(self.existing)


@dataclass(frozen=True)
class VideoFile:
    """A downloaded video file (e.g. a TikTok clip) ready to be sent."""

    path: str
    title: Optional[str] = None
    uploader: Optional[str] = None
    duration: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    thumb_path: Optional[str] = None

    @property
    def exists(self) -> bool:
        return bool(self.path) and os.path.exists(self.path)

    @property
    def size(self) -> int:
        return os.path.getsize(self.path) if self.exists else 0

    @property
    def filename(self) -> str:
        return os.path.basename(self.path)
