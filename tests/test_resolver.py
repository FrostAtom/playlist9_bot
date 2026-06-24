"""Tests for the shared input classifier (app.music.resolver).

This is the single source of truth for "what kind of thing did the user paste?",
used by the bot's on_text, inline mode, and the web /api/search. The decision
tree is pure and synchronous, so it's exhaustively testable offline.
"""
from __future__ import annotations

import pytest

from app.music import resolver
from app.music.links import ExternalItem
from app.music.resolver import InputKind, classify, external_items_to_tracks


# ───────────────────────── TikTok ─────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        "https://www.tiktok.com/@someone/video/7234567890123456789",
        "https://www.tiktok.com/@a.b-c/photo/7234567890123456789",
        "https://vm.tiktok.com/ZMabc123/",
        "https://vt.tiktok.com/ZSdef456/",
        "look at this https://www.tiktok.com/t/ZTxyz/ lol",
    ],
)
def test_tiktok_links(service, text):
    result = classify(service, text)
    assert result.kind is InputKind.TIKTOK
    assert result.tiktok_url and "tiktok.com" in result.tiktok_url


# ───────────────────── YouTube / SoundCloud ─────────────────────

def test_youtube_single_track(service):
    r = classify(service, "https://music.youtube.com/watch?v=dQw4w9WgXcQ")
    assert r.kind is InputKind.LINK_TRACK
    assert r.source == "youtube"
    assert r.track_url == "https://music.youtube.com/watch?v=dQw4w9WgXcQ"
    assert r.playlist_url is None


def test_youtube_playlist(service):
    r = classify(service, "https://www.youtube.com/playlist?list=PL1234567890")
    assert r.kind is InputKind.LINK_PLAYLIST
    assert r.source == "youtube"
    assert r.playlist_url and "list=PL1234567890" in r.playlist_url
    assert r.track_url is None


def test_youtube_ambiguous_track_in_playlist(service):
    r = classify(service, "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1234567890")
    assert r.kind is InputKind.LINK_AMBIGUOUS
    assert r.track_url and "v=dQw4w9WgXcQ" in r.track_url
    assert r.playlist_url and "list=PL1234567890" in r.playlist_url


def test_youtube_virtual_radio_list_is_a_track(service):
    # An auto-generated radio/mix list (RD…) can't be enumerated, so a watch link
    # carrying one is treated as a plain single track.
    r = classify(service, "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ")
    assert r.kind is InputKind.LINK_TRACK
    assert r.playlist_url is None


def test_soundcloud_track(service):
    r = classify(service, "https://soundcloud.com/some-artist/some-track")
    assert r.kind is InputKind.LINK_TRACK
    assert r.source == "soundcloud"
    assert r.track_url == "https://soundcloud.com/some-artist/some-track"


def test_soundcloud_set_is_playlist(service):
    r = classify(service, "https://soundcloud.com/some-artist/sets/my-set")
    assert r.kind is InputKind.LINK_PLAYLIST
    assert r.source == "soundcloud"
    assert r.playlist_url and "/sets/" in r.playlist_url
    assert r.track_url is None


def test_link_url_is_populated_for_links(service):
    r = classify(service, "https://soundcloud.com/some-artist/some-track")
    assert r.link_url == "https://soundcloud.com/some-artist/some-track"


# ───────────────────── Spotify / Apple Music ─────────────────────

def test_spotify_track(service):
    r = classify(service, "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")
    assert r.kind is InputKind.EXTERNAL_TRACK
    assert r.external_url and "open.spotify.com/track/" in r.external_url


def test_spotify_playlist(service):
    r = classify(service, "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
    assert r.kind is InputKind.EXTERNAL_PLAYLIST


def test_spotify_album(service):
    r = classify(service, "https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3")
    assert r.kind is InputKind.EXTERNAL_PLAYLIST


def test_spotify_intl_prefix_track(service):
    r = classify(service, "https://open.spotify.com/intl-de/track/4cOdK2wGLETKBW3PvgPWqT")
    assert r.kind is InputKind.EXTERNAL_TRACK


def test_apple_playlist(service):
    r = classify(service, "https://music.apple.com/us/playlist/some-name/pl.u-abc123")
    assert r.kind is InputKind.EXTERNAL_PLAYLIST


def test_apple_album(service):
    r = classify(service, "https://music.apple.com/us/album/some-album/1440855162")
    assert r.kind is InputKind.EXTERNAL_PLAYLIST


def test_apple_single_track_in_album(service):
    # The same album URL with ?i=<id> targets one song, not the whole album.
    r = classify(service, "https://music.apple.com/us/album/some-album/1440855162?i=1440855170")
    assert r.kind is InputKind.EXTERNAL_TRACK


# ───────────────────────── plain search ─────────────────────────

@pytest.mark.parametrize(
    "text",
    ["daft punk instant crush", "hello world", "just some words", "track by artist"],
)
def test_plain_search(service, text):
    r = classify(service, text)
    assert r.kind is InputKind.SEARCH
    assert r.text == text


def test_classify_strips_whitespace(service):
    r = classify(service, "   daft punk   ")
    assert r.kind is InputKind.SEARCH
    assert r.text == "daft punk"


def test_priority_tiktok_over_search_text(service):
    # A TikTok link embedded in extra text still classifies as TikTok.
    r = classify(service, "check https://www.tiktok.com/@x/video/7234567890123456789 now")
    assert r.kind is InputKind.TIKTOK


# ───────────────────── external_items_to_tracks ─────────────────────

def test_external_items_to_tracks():
    items = [
        ExternalItem(query="Daft Punk Get Lucky", title="Get Lucky", artist="Daft Punk"),
        ExternalItem(query="A-ha Take On Me", title="Take On Me", artist="A-ha"),
    ]
    tracks = external_items_to_tracks(items, "youtube")
    assert len(tracks) == 2
    assert tracks[0].id == "q0"
    assert tracks[1].id == "q1"
    assert tracks[0].title == "Get Lucky"
    assert tracks[0].uploader == "Daft Punk"
    assert tracks[0].query == "Daft Punk Get Lucky"
    assert tracks[0].url == ""           # no direct URL — resolved at download time
    assert tracks[0].source == "youtube"


def test_external_items_to_tracks_blank_artist_is_none():
    items = [ExternalItem(query="Some Title", title="Some Title", artist="")]
    tracks = external_items_to_tracks(items, "youtube")
    assert tracks[0].uploader is None


def test_external_items_to_tracks_empty():
    assert external_items_to_tracks([], "youtube") == []
