"""Tests for the pure helpers in the web API layer (app.web.api)."""
from __future__ import annotations

from urllib.parse import quote

from app.web import api


# ───────────────────────── source_name ─────────────────────────

def test_source_name_known():
    assert api.source_name("youtube") == "YouTube Music"
    assert api.source_name("soundcloud") == "SoundCloud"


def test_source_name_unknown_falls_back_to_title():
    assert api.source_name("bandcamp") == "Bandcamp"


# ───────────────────── _attachment (RFC 5987) ─────────────────────

def test_attachment_ascii():
    h = api._attachment("track.mp3")
    assert h == "attachment; filename=\"track.mp3\"; filename*=UTF-8''track.mp3"


def test_attachment_unicode_keeps_ascii_fallback_and_encodes():
    name = "Пісня - Назва.mp3"
    h = api._attachment(name)
    # ASCII fallback strips the non-ASCII letters but keeps the structure...
    assert 'filename="' in h and h.endswith(quote(name))
    # ...and the UTF-8 part is the percent-encoded original.
    assert "filename*=UTF-8''" + quote(name) in h


def test_attachment_all_nonascii_uses_default_fallback():
    # No ASCII chars at all → the quoted fallback must not be empty.
    h = api._attachment("Пісня")
    assert 'filename="track.mp3"' in h
    assert h.endswith(quote("Пісня"))


def test_attachment_has_both_forms():
    h = api._attachment("Café del Mar.mp3")
    assert h.startswith("attachment; filename=\"")
    assert "filename*=UTF-8''" in h
