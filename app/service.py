"""Facade that routes search/download requests across registered sources."""
from __future__ import annotations

from typing import List, Optional, Tuple, Union

from .models import AudioFile, Meta, Track
from .sources.base import AudioSource


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

    async def search(self, query: str, limit: int, source: str) -> List[Track]:
        src = self._by_name.get(source)
        return await src.search(query, limit) if src else []

    async def download(self, ref: Union[Track, str], workdir: str) -> AudioFile:
        """Download a picked :class:`Track` (with metadata) or a raw URL."""
        if isinstance(ref, Track):
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
