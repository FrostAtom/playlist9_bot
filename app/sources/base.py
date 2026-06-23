"""Abstract audio source.

Adding support for a new platform is a matter of implementing this interface
(or subclassing :class:`~app.sources.ytdlp_source.YtDlpSource`) and registering
the source in ``build_service`` (see ``app/application.py``).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from ..models import AudioFile, Meta, Track


class AudioSource(ABC):
    #: Human-readable identifier, e.g. "youtube".
    name: str = "source"
    #: Whether this source participates in cross-platform search.
    searchable: bool = True

    @abstractmethod
    def handles(self, text: str) -> Optional[str]:
        """Return a canonical URL if this source recognizes the text, else None."""

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
