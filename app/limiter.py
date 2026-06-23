"""Concurrency limits for downloads: per-user and global."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Dict


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
