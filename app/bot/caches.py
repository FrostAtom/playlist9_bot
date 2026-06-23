"""In-memory, per-user interaction state.

The counterpart to the persistent file_id store (``store.py``): this holds
short-lived state that needn't survive a restart — recent searches (for
pagination / source toggle) and the tracks offered in a given inline query.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..models import Track


# How a paginated result set behaves. "search": cross-source query (toggle +
# direct picks). "url_playlist": yt/sc playlist, picks download directly.
# "query_playlist": Spotify/Apple playlist, picks search YouTube Music first.
SEARCH = "search"
URL_PLAYLIST = "url_playlist"
QUERY_PLAYLIST = "query_playlist"


@dataclass
class SearchState:
    """A paginated result set: a search, or the tracks of a pasted playlist."""

    query: str
    source: str
    tracks: List[Track]
    kind: str = SEARCH
    #: Playlist title, shown in the header (None for plain searches).
    title: Optional[str] = None

    @property
    def is_playlist(self) -> bool:
        return self.kind != SEARCH


@dataclass(frozen=True)
class PendingLink:
    """An ambiguous link awaiting the user's track-or-playlist choice."""

    source: str
    track_url: str
    playlist_url: str


class SearchCache:
    """Per-user store of recent searches, keyed by results message id."""

    def __init__(self, per_user: int = 20) -> None:
        self._per_user = per_user
        self._data: Dict[int, "OrderedDict[str, SearchState]"] = {}

    def save(self, user_id: int, token: str, state: SearchState) -> None:
        store = self._data.setdefault(user_id, OrderedDict())
        store[token] = state
        while len(store) > self._per_user:
            store.popitem(last=False)

    def load(self, user_id: int, token: str) -> Optional[SearchState]:
        return self._data.get(user_id, {}).get(token)


class LinkCache:
    """Per-user store of ambiguous links awaiting a track/playlist choice,
    keyed by the prompt message id (the same token scheme as searches)."""

    def __init__(self, per_user: int = 20) -> None:
        self._per_user = per_user
        self._data: Dict[int, "OrderedDict[str, PendingLink]"] = {}

    def save(self, user_id: int, token: str, link: PendingLink) -> None:
        store = self._data.setdefault(user_id, OrderedDict())
        store[token] = link
        while len(store) > self._per_user:
            store.popitem(last=False)

    def load(self, user_id: int, token: str) -> Optional[PendingLink]:
        return self._data.get(user_id, {}).get(token)


class TrackCache:
    """Short-lived store of tracks offered inline, keyed by inline result id,
    so a chosen result can be downloaded and delivered."""

    def __init__(self, capacity: int = 1000) -> None:
        self._cap = capacity
        self._data: "OrderedDict[str, Track]" = OrderedDict()

    def put(self, key: str, track: Track) -> None:
        self._data[key] = track
        self._data.move_to_end(key)
        while len(self._data) > self._cap:
            self._data.popitem(last=False)

    def get(self, key: str) -> Optional[Track]:
        return self._data.get(key)
