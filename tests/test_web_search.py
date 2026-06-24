"""Integration-ish tests for the web /api/search dispatch.

These exercise the refactored endpoint end-to-end against an offline stub
service, proving the shared resolver is wired into the web layer correctly —
without any network. Branches that require scraping/searching the network
(EXTERNAL_*) are covered by the resolver's own classification tests.
"""
from __future__ import annotations

import json
from typing import List, Optional, Tuple

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from app.config import Settings
from app.models import Track
from app.web import api


class StubService:
    """Minimal MusicService stand-in for the search/playlist branches."""

    def __init__(self) -> None:
        self.searched: List[Tuple[str, int, str]] = []
        self.playlisted: List[Tuple[str, str]] = []

    def resolve(self, text: str):
        # Pretend only youtube.com URLs are recognised links.
        if "youtube.com" in text:
            return (_FakeSource("youtube"), text)
        return None

    def link_info(self, text: str):
        from app.music.service import LinkInfo

        if "list=" in text and "v=" not in text:
            return LinkInfo("youtube", None, text)
        return LinkInfo("youtube", text, None)

    def searchable_sources(self) -> List[str]:
        return ["youtube", "soundcloud"]

    def default_source(self) -> str:
        return "youtube"

    async def search(self, query: str, limit: int, source: str) -> List[Track]:
        self.searched.append((query, limit, source))
        return [
            Track(id="vid1", title="Found", url="https://music.youtube.com/watch?v=vid1",
                  uploader="Artist", duration=200, source=source)
        ]

    async def resolve_track(self, url: str) -> Optional[Track]:
        return Track(id="vid1", title="Resolved", url=url, uploader="Artist", source="youtube")

    async def playlist(self, url: str, limit: int, source: str):
        self.playlisted.append((url, source))
        return ([Track(id="p1", title="P1", url="u1", source=source)], "My Playlist")


class _FakeSource:
    def __init__(self, name: str) -> None:
        self.name = name


def _make_request(query: str, source: str = "youtube") -> web.Request:
    app = web.Application()
    app["service"] = StubService()
    app["settings"] = Settings(token="t")
    from urllib.parse import quote
    return make_mocked_request(
        "GET", f"/api/search?q={quote(query)}&source={source}", app=app
    )


async def _call(query: str, source: str = "youtube"):
    req = _make_request(query, source)
    resp = await api.search(req)
    return resp, json.loads(resp.body)


async def test_search_plain_text():
    resp, data = await _call("daft punk")
    assert resp.status == 200
    assert data["kind"] == "search"
    assert data["source"] == "youtube"
    assert data["tracks"][0]["title"] == "Found"


async def test_search_empty_query():
    resp, data = await _call("")
    assert resp.status == 200
    assert data["tracks"] == []


async def test_search_unknown_source_falls_back_to_default():
    resp, data = await _call("daft punk", source="bogus")
    assert data["source"] == "youtube"


async def test_search_youtube_single_link_resolves_track():
    resp, data = await _call("https://music.youtube.com/watch?v=vid1")
    assert data["kind"] == "search"
    assert len(data["tracks"]) == 1
    assert data["tracks"][0]["title"] == "Resolved"


async def test_search_youtube_playlist_link_enumerates():
    resp, data = await _call("https://www.youtube.com/playlist?list=PL123")
    assert data["kind"] == "playlist"
    assert data["title"] == "My Playlist"
    assert data["tracks"][0]["title"] == "P1"


async def test_search_tiktok_link_is_rejected():
    resp, data = await _call("https://www.tiktok.com/@x/video/7234567890123456789")
    assert resp.status == 415
    assert "error" in data
