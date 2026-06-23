"""Persistent ``file_id`` cache backed by PostgreSQL (asyncpg).

Telegram ``file_id``s are permanent, so remembering them across restarts lets the
bot re-send a previously delivered track instantly — no second download — and
makes inline mode answer with playable audio far more often.

A small in-memory LRU sits in front of the database so hot lookups (every inline
keystroke probes the cache for each result) don't hit Postgres. The database is
required: if it is unreachable at startup the bot refuses to start (the container
then restarts and retries) rather than silently losing persistence.
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_ids (
    key        TEXT PRIMARY KEY,
    file_id    TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class FileIdStore:
    """Maps ``"source:track_id"`` to a Telegram ``file_id``.

    Persisted in Postgres when a pool is provided; otherwise purely in-memory.
    """

    def __init__(self, pool=None, mem_capacity: int = 500) -> None:
        self._pool = pool
        self._cap = mem_capacity
        self._mem: "OrderedDict[str, str]" = OrderedDict()

    @staticmethod
    def key(source: str, track_id: str) -> str:
        return f"{source}:{track_id}"

    async def get(self, key: str) -> Optional[str]:
        cached = self._mem.get(key)
        if cached is not None:
            self._mem.move_to_end(key)
            return cached
        if self._pool is None:
            return None
        try:
            row = await self._pool.fetchrow(
                "SELECT file_id FROM file_ids WHERE key = $1", key
            )
        except Exception:  # noqa: BLE001 - a cache miss must never break delivery
            logger.warning("file_id store read failed for %s", key, exc_info=True)
            return None
        if row:
            self._remember(key, row["file_id"])
            return row["file_id"]
        return None

    async def get_many(self, keys: List[str]) -> Dict[str, str]:
        """Resolve several keys at once (one DB round-trip for the misses)."""
        found: Dict[str, str] = {}
        missing: List[str] = []
        for key in keys:
            cached = self._mem.get(key)
            if cached is not None:
                self._mem.move_to_end(key)
                found[key] = cached
            else:
                missing.append(key)
        if missing and self._pool is not None:
            try:
                rows = await self._pool.fetch(
                    "SELECT key, file_id FROM file_ids WHERE key = ANY($1::text[])",
                    missing,
                )
            except Exception:  # noqa: BLE001
                logger.warning("file_id store batch read failed", exc_info=True)
                rows = []
            for row in rows:
                self._remember(row["key"], row["file_id"])
                found[row["key"]] = row["file_id"]
        return found

    async def put(self, key: str, file_id: str) -> None:
        self._remember(key, file_id)
        if self._pool is None:
            return
        try:
            await self._pool.execute(
                "INSERT INTO file_ids (key, file_id) VALUES ($1, $2) "
                "ON CONFLICT (key) DO UPDATE SET "
                "file_id = EXCLUDED.file_id, updated_at = now()",
                key,
                file_id,
            )
        except Exception:  # noqa: BLE001 - persistence is best-effort
            logger.warning("file_id store write failed for %s", key, exc_info=True)

    def _remember(self, key: str, file_id: str) -> None:
        self._mem[key] = file_id
        self._mem.move_to_end(key)
        while len(self._mem) > self._cap:
            self._mem.popitem(last=False)


async def create_pool(
    dsn: str,
    *,
    password: Optional[str] = None,
    retries: int = 10,
    delay: float = 2.0,
):
    """Create an asyncpg pool and ensure the schema, retrying while the database
    starts up (compose brings it up alongside the bot).

    ``password`` is applied separately from the DSN (so special characters can't
    corrupt the URL); when falsy, the DSN is used as-is. The database is
    mandatory: if it never becomes reachable this raises ``SystemExit`` so the
    container exits and is restarted by Docker — we never run without persistence.
    """
    try:
        import asyncpg
    except ImportError:
        raise SystemExit("asyncpg is not installed but DATABASE_URL is set")

    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            pool = await asyncpg.create_pool(
                dsn, password=password or None, min_size=1, max_size=5
            )
            async with pool.acquire() as conn:
                await conn.execute(_SCHEMA)
            logger.info("Connected to PostgreSQL file_id store")
            return pool
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.info("Waiting for PostgreSQL (%d/%d): %s", attempt, retries, exc)
            await asyncio.sleep(delay)
    raise SystemExit(f"PostgreSQL unreachable after {retries} attempts: {last_error}")
