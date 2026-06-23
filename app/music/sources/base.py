"""Abstract audio source.

Adding support for a new platform is a matter of implementing this interface
(or subclassing :class:`~app.sources.ytdlp_source.YtDlpSource`) and registering
the source in ``build_service`` (see ``app/application.py``).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from ...models import AudioFile, Meta, Track


class AudioSource(ABC):
    #: Human-readable identifier, e.g. "youtube".
    name: str = "source"
    #: Whether this source participates in cross-platform search.
    searchable: bool = True

    @abstractmethod
    def handles(self, text: str) -> Optional[str]:
        """Return a canonical URL if this source recognizes the text, else None."""

    def track_url(self, text: str) -> Optional[str]:
        """A single downloadable-track URL from ``text``, or None.

        For most links this is the link itself; sources override when a link can
        point at a playlist with no single track (e.g. a SoundCloud set)."""
        return self.handles(text)

    def playlist_url(self, text: str) -> Optional[str]:
        """A URL to enumerate as a playlist, or None when ``text`` isn't one.

        Returning a value alongside a ``track_url`` means the link is ambiguous
        (e.g. a YouTube ``watch?v=…&list=…``) and the user should be asked which
        they meant."""
        return None

    async def list_playlist(
        self, url: str, limit: int
    ) -> Tuple[List[Track], Optional[str]]:
        """Return up to ``limit`` tracks of the playlist at ``url`` and its title.

        Sources that can't enumerate playlists return ``([], None)``."""
        return [], None

    @abstractmethod
    async def search(self, query: str, limit: int) -> List[Track]:
        """Search the source and return up to ``limit`` tracks."""

    @abstractmethod
    async def download(
        self, url: str, workdir: str, meta: Optional[Meta] = None
    ) -> AudioFile:
        """Download the audio for ``url`` into ``workdir``.

        ``meta`` carries authoritative tags (from search) to embed; when None,
        tags are derived from the downloaded file itself.
        """
