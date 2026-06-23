"""A tiny status page: live metrics + recent error logs.

The bot polls Telegram and exposes no HTTP port, so this is the only window
into a running instance without shelling into the container. It serves a single
self-contained HTML page that polls ``/api/stats`` (the JSON snapshot from
``metrics``) every few seconds — no build step, no assets, no auth (bind it to a
private network / reverse proxy if you expose it).

``aiohttp`` is already an aiogram dependency, so this adds no new packages.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional

from aiohttp import web

from ..infra.metrics import Metrics

logger = logging.getLogger(__name__)

# yt-dlp uses date-based versions (YYYY.MM.DD). YouTube periodically breaks older
# releases ("No video formats found"), so once the installed build is older than
# this many days the status page nudges to bump the pin in requirements.txt.
_YTDLP_STALE_DAYS = 30

# The page markup lives next to this module as a plain file (no templating
# engine) — its only dynamic part is the JSON-encoded counter labels below.
_TEMPLATE = Path(__file__).parent / "templates" / "status.html"

# Friendly labels + display order for the counters incremented around the app.
# Counters not listed here still show up (raw key) so new metrics aren't lost.
_COUNTER_LABELS = {
    "searches": "Searches",
    "inline_queries": "Inline queries",
    "links_resolved": "External links",
    "playlists": "Playlists opened",
    "downloads_ok": "Downloads sent",
    "downloads_failed": "Downloads failed",
    "sends_failed": "Sends failed",
    "rate_limited": "Rate-limited",
}

# Rendered once on first request, then served from memory (the file is static).
_page_cache: str | None = None


def _render_page() -> str:
    global _page_cache
    if _page_cache is None:
        markup = _TEMPLATE.read_text(encoding="utf-8")
        _page_cache = markup.replace("__LABELS__", json.dumps(_COUNTER_LABELS))
    return _page_cache

def _parse_ytdlp_date(version: str) -> Optional[date]:
    parts = version.split(".")
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return None  # nightly/dev or unexpected format — skip the age check


def _ytdlp_status() -> dict:
    """Installed yt-dlp version + how stale it is (for the update nudge)."""
    try:
        from yt_dlp.version import __version__ as version
    except Exception:  # noqa: BLE001 - never let diagnostics break the page
        version = None
    released = _parse_ytdlp_date(version) if version else None
    age = (date.today() - released).days if released else None
    return {
        "version": version,
        "released": released.isoformat() if released else None,
        "age_days": age,
        "stale": age is not None and age >= _YTDLP_STALE_DAYS,
        "stale_after_days": _YTDLP_STALE_DAYS,
    }


async def _index(_request: web.Request) -> web.Response:
    return web.Response(text=_render_page(), content_type="text/html")


async def _stats(request: web.Request) -> web.Response:
    metrics: Metrics = request.app["metrics"]
    data = metrics.snapshot()
    data["ytdlp"] = _ytdlp_status()
    return web.json_response(data)


async def _healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def start_web_server(metrics: Metrics, host: str, port: int) -> web.AppRunner:
    """Start the status server and return its runner (caller cleans it up).

    Failures to bind are logged and swallowed: the metrics page is an aid, never
    a reason to take the bot down.
    """
    app = web.Application()
    app["metrics"] = metrics
    app.add_routes(
        [
            web.get("/", _index),
            web.get("/api/stats", _stats),
            web.get("/healthz", _healthz),
        ]
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Status page listening on http://%s:%d", host, port)
    return runner
