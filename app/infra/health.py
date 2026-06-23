"""Liveness heartbeat written by the running event loop.

The bot uses long-polling (no HTTP port), so the Docker healthcheck instead
verifies that this heartbeat file is being refreshed — i.e. the asyncio loop is
alive, not just the process.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

HEARTBEAT_FILE = Path("/tmp/heartbeat")
INTERVAL = 15


async def heartbeat() -> None:
    while True:
        try:
            HEARTBEAT_FILE.write_text(str(int(time.time())))
        except OSError:
            logger.warning("Could not write heartbeat", exc_info=True)
        await asyncio.sleep(INTERVAL)
