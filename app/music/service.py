"""Facade that routes search/download requests across registered sources."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

from ..models import AudioFile, Meta, Track
from .sources.base import AudioSource


@dataclass(frozen=True)
class LinkInfo:
    """What a recognized link points at: a single track, a playlist, or both."""

    source: str
    track_url: Optional[str]
    playlist_url: Optional[str]

    @property
    def ambiguous(self) -> bool:
        """A link that is both a track and a playlist (ask the user which)."""
        return bool(self.track_url and self.playlist_url)


class MusicService:
    def __init__(self, sources: List[AudioSource]) -> None:
        if not sources:
            raise ValueError("MusicService requires at least one source")
        self._sources = sources
        self._by_name = {s.name: s for s in sources}

    @property
    def default(self) -> AudioSource:
        return self._sources[0]

    def searchable_sources(self) -> List[str]:
        return [s.name for s in self._sources if s.searchable]

    def default_source(self) -> str:
        names = self.searchable_sources()
        return names[0] if names else ""

    def next_source(self, current: str) -> str:
        """The next searchable source, cycling (used by the toggle button)."""
        names = self.searchable_sources()
        if not names:
            return ""
        if current not in names:
            return names[0]
        return names[(names.index(current) + 1) % len(names)]

    def resolve(self, text: str) -> Optional[Tuple[AudioSource, str]]:
        """Find the source that recognizes ``text`` as a URL."""
        for source in self._sources:
            url = source.handles(text)
            if url:
                return source, url
        return None

    async def resolve_track(self, text: str) -> Optional[Track]:
        """Introspect a recognized single-track URL into a :class:`Track`, or None."""
        match = self.resolve(text)
        if not match:
            return None
        source, url = match
        return await source.resolve_track(url)

    def link_info(self, text: str) -> Optional[LinkInfo]:
        """Classify a recognized link as a track and/or playlist, or None."""
        for source in self._sources:
            if source.handles(text):
                return LinkInfo(
                    source=source.name,
                    track_url=source.track_url(text),
                    playlist_url=source.playlist_url(text),
                )
        return None

    async def playlist(
        self, url: str, limit: int, source: str
    ) -> Tuple[List[Track], Optional[str]]:
        """Enumerate up to ``limit`` tracks of a playlist URL on ``source``."""
        src = self._by_name.get(source)
        return await src.list_playlist(url, limit) if src else ([], None)

    async def search(self, query: str, limit: int, source: str) -> List[Track]:
        src = self._by_name.get(source)
        return await src.search(query, limit) if src else []

    async def download(self, ref: Union[Track, str], workdir: str) -> AudioFile:
        """Download a picked :class:`Track` (with metadata) or a raw URL."""
        if isinstance(ref, Track):
            # A playlist item with only a search query (Spotify/Apple) carries no
            # direct URL — find the concrete track on its source first.
            if not ref.url and ref.query:
                matches = await self.search(ref.query, 1, ref.source)
                if not matches:
                    raise RuntimeError(f"No match found for {ref.query!r}")
                ref = matches[0]
            source = self._by_name.get(ref.source)
            url = ref.url
            if source is None:
                match = self.resolve(ref.url)
                source, url = match if match else (self.default, ref.url)
            meta = Meta(
                title=ref.title,
                artist=ref.uploader or "",
                album=ref.album,
                cover_url=ref.cover_url,
                duration=ref.duration,
            )
            return await source.download(url, workdir, meta)

        match = self.resolve(ref)
        source, url = match if match else (self.default, ref)
        return await source.download(url, workdir, None)
