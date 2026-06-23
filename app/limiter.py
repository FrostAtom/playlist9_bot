"""Concurrency limits for downloads: per-user and global."""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Deque, Dict


class RateLimiter:
    """Per-user sliding-window throttle (e.g. at most 10 downloads per 60 s).

    ``allow`` records the event and returns whether it was within the limit;
    ``retry_after`` reports how long until the oldest event in the window ages
    out, for a friendly "try again in Ns" message.
    """

    def __init__(self, max_events: int = 10, window: float = 60.0) -> None:
        self._max = max_events
        self._window = window
        self._events: Dict[int, Deque[float]] = defaultdict(deque)

    def _prune(self, queue: Deque[float], now: float) -> None:
        cutoff = now - self._window
        while queue and queue[0] <= cutoff:
            queue.popleft()

    def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        queue = self._events[user_id]
        self._prune(queue, now)
        if len(queue) >= self._max:
            return False
        queue.append(now)
        return True

    def retry_after(self, user_id: int) -> int:
        queue = self._events.get(user_id)
        if not queue:
            return 0
        return max(1, int(self._window - (time.monotonic() - queue[0])) + 1)


class DownloadLimiter:
    def __init__(self, per_user: int = 3, total: int = 8) -> None:
        self._per_user = per_user
        self._total = asyncio.Semaphore(total)
        self._users: Dict[int, asyncio.Semaphore] = {}

    def _user_sem(self, user_id: int) -> asyncio.Semaphore:
        sem = self._users.get(user_id)
        if sem is None:
            sem = asyncio.Semaphore(self._per_user)
            self._users[user_id] = sem
        return sem

    def busy(self, user_id: int) -> bool:
        """True if acquiring a slot would block (used to show a queue notice)."""
        return self._user_sem(user_id).locked() or self._total.locked()

    @asynccontextmanager
    async def slot(self, user_id: int):
        user = self._user_sem(user_id)
        async with user, self._total:
            yield
