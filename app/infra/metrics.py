"""In-process metrics: counters, unique-user activity, and a recent-log buffer.

The bot is a long-polling process with no HTTP surface of its own, so there is
nowhere to look when something goes wrong short of ``docker logs``. This module
collects lightweight runtime signals that ``web.py`` renders as a small status
page. Everything lives in memory and resets on restart — no database, no extra
dependencies.

The three signals are deliberately split into small single-responsibility
collectors (``_Counters``, ``_ActiveUsers``, ``_LogBuffer``), each owning its own
lock, and composed behind the ``Metrics`` facade. A single process-wide
``metrics`` instance is imported wherever an event is worth recording.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Deque, Dict, List

DAY_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class LogEntry:
    ts: float          # epoch seconds (record.created)
    level: str         # e.g. "ERROR"
    logger: str        # logger name
    message: str       # formatted message (includes traceback for exceptions)


class _Counters:
    """A thread-safe monotonic integer counter map."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._values: Dict[str, int] = {}

    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._values[name] = self._values.get(name, 0) + amount

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._values)


class _ActiveUsers:
    """Last-seen timestamp per user, for a rolling unique-user count.

    Memory stays bounded to *recently active* users: each count() prunes anyone
    whose last activity has aged out of the window."""

    def __init__(self, window: float = DAY_SECONDS) -> None:
        self._lock = Lock()
        self._window = window
        self._last_seen: Dict[int, float] = {}

    def seen(self, user_id: int, now: float) -> None:
        with self._lock:
            self._last_seen[user_id] = now

    def count(self, now: float) -> int:
        cutoff = now - self._window
        with self._lock:
            self._last_seen = {
                uid: ts for uid, ts in self._last_seen.items() if ts >= cutoff
            }
            return len(self._last_seen)


class _LogBuffer:
    """A bounded ring buffer of recent log records (newest returned first)."""

    def __init__(self, capacity: int = 200) -> None:
        self._lock = Lock()
        self._entries: Deque[LogEntry] = deque(maxlen=capacity)

    def add(self, entry: LogEntry) -> None:
        with self._lock:
            self._entries.append(entry)

    def recent(self) -> List[LogEntry]:
        with self._lock:
            return list(reversed(self._entries))


class Metrics:
    """Facade composing the individual metric collectors."""

    def __init__(
        self, log_capacity: int = 200, user_window: float = DAY_SECONDS
    ) -> None:
        self._start = time.time()
        self._counters = _Counters()
        self._users = _ActiveUsers(user_window)
        self._logs = _LogBuffer(log_capacity)

    def incr(self, name: str, amount: int = 1) -> None:
        self._counters.incr(name, amount)

    def seen_user(self, user_id: int) -> None:
        self._users.seen(user_id, time.time())

    def record_log(self, entry: LogEntry) -> None:
        self._logs.add(entry)

    def snapshot(self) -> dict:
        """A JSON-serialisable view of current state for the status API."""
        now = time.time()
        return {
            "started_at": self._start,
            "uptime_seconds": int(now - self._start),
            "unique_users_24h": self._users.count(now),
            "counters": self._counters.snapshot(),
            "errors": [
                {"ts": e.ts, "level": e.level, "logger": e.logger, "message": e.message}
                for e in self._logs.recent()
            ],
        }


# Process-wide singleton; import and use directly.
metrics = Metrics()


class MetricsLogHandler(logging.Handler):
    """Logging handler that funnels WARNING+ records into ``Metrics``.

    Installed on the root logger at startup so any module's
    ``logger.warning``/``logger.exception`` surfaces on the status page.
    """

    def __init__(self, sink: Metrics, level: int = logging.WARNING) -> None:
        super().__init__(level)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink.record_log(
                LogEntry(
                    ts=record.created,
                    level=record.levelname,
                    logger=record.name,
                    message=self.format(record),
                )
            )
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)
