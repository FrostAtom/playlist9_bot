"""Shared pytest fixtures.

Tests run inside the Docker image (all runtime deps present) but must stay
offline and deterministic — no network, no downloads. The fixtures here build
real domain objects whose *construction* touches no network (YouTube Music /
SoundCloud sources resolve URLs with regex; the ytmusicapi client is created
lazily only on the first actual search, which the tests never trigger).
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.music.service import MusicService
from app.music.sources.soundcloud import SoundCloudSource
from app.music.sources.youtube import YouTubeMusicSource


@pytest.fixture
def service() -> MusicService:
    """A real MusicService with both sources — offline-safe to construct."""
    return MusicService([YouTubeMusicSource(), SoundCloudSource()])


@pytest.fixture
def settings() -> Settings:
    return Settings(token="test")
