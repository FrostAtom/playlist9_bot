"""In-memory, per-user interaction state.

The counterpart to the persistent file_id store (``store.py``): this holds
short-lived state that needn't survive a restart — recent searches (for
pagination / source toggle) and the tracks offered in a given inline query.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional

from .models import Track


@dataclass
class SearchState:
    """A search whose results are paginated and can be re-run on another source."""

    query: str
    source: str
    tracks: List[Track]


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
