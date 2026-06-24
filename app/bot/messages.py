"""User-facing text. Centralized so wording/localization lives in one place."""
from __future__ import annotations

WELCOME = (
    "🎵 *Music Downloader*\n\n"
    "• Send a *track name* — I'll search YouTube Music 🎵 (tap ⇄ to switch the "
    "search to SoundCloud ☁️) and let you pick.\n"
    "• Or send a *link* — YouTube/SoundCloud download directly; Spotify & Apple "
    "Music links are matched on YouTube Music.\n"
    "• Send a *playlist or album link* — grab the tracks one by one "
    "(YouTube, SoundCloud, Spotify, Apple Music).\n"
    "• Send a *TikTok link* — I'll send back the video 🎬\n"
    "• In any chat, type `@bot_name query` for inline search — or paste a "
    "*track, playlist or TikTok link* there too.\n\n"
    "Files arrive at up to 320 kbps with cover art and clean tags.\n\n"
    "📂 [Source on GitHub](https://github.com/FrostAtom/playlist9_bot)"
)

DOWNLOADING = "⏳ Downloading audio, please wait..."
DOWNLOADING_VIDEO = "⏳ Downloading the video, please wait..."
DOWNLOADING_CHOICE = "⏳ Downloading the selected track, please wait..."
RESOLVING_LINK = "🔗 Reading the link..."
LOADING_PLAYLIST = "📋 Reading the playlist..."
PLAYLIST_FAILED = "❌ Couldn't read that playlist. Try sending a track instead."
PLAYLIST_EMPTY = "The playlist looks empty 😔"
# Prompt shown when a link is both a single track and a playlist.
PLAYLIST_PROMPT = "🔗 This link is part of a playlist. What should I grab?"
BTN_ONLY_TRACK = "🎵 Just this track"
BTN_WHOLE_PLAYLIST = "📋 The whole playlist"
QUEUED = "⏳ Queued — the concurrent download limit has been reached..."
UPLOADING = "📤 Uploading the file..."
NO_AUDIO = "❌ Could not extract audio from the link."
NO_VIDEO = "❌ Could not download a video from that link."
NOT_FOUND = "Nothing found 😔 Try a different query."
LINK_FAILED = "❌ Couldn't read that link. Try sending the track name instead."
RESULTS_EXPIRED = "Results expired, please search again 🔁"
SEARCH_ERROR = "Search error, please try again 🔁"

# Inline placeholder shown for a pasted TikTok link (before the clip downloads).
INLINE_TIKTOK_TITLE = "🎬 TikTok video"
INLINE_TIKTOK_DESC = "Tap to download and send the clip"


def link_not_found(provider: str) -> str:
    return f"Couldn't find that {provider} track to download 😔"


def rate_limited(per_minute: int, retry_after: int) -> str:
    return (
        f"⏳ Slow down — up to {per_minute} downloads per minute. "
        f"Try again in {retry_after}s."
    )


def results_header(total: int, page: int, pages: int, source_label: str) -> str:
    text = f"{source_label} · found: {total}"
    if pages > 1:
        text += f" · page {page + 1}/{pages}"
    return text


def playlist_header(title: str, total: int, page: int, pages: int) -> str:
    text = f"📋 {title} · {total} tracks"
    if pages > 1:
        text += f" · page {page + 1}/{pages}"
    text += "\nTap a track to download it."
    return text


def nothing_on(source_label: str) -> str:
    return f"Nothing found on {source_label} 😔"


def searching(query: str) -> str:
    return f"🔎 Searching: «{query}»..."


def download_failed(error: object) -> str:
    return f"❌ Download failed: {error}"


def send_failed(error: object) -> str:
    return f"❌ Failed to send: {error}"


def search_failed(error: object) -> str:
    return f"❌ Search error: {error}"


def too_large(size_bytes: int, limit_bytes: int) -> str:
    mb = size_bytes // (1024 * 1024)
    limit_mb = limit_bytes // (1024 * 1024)
    return (
        f"❌ The file is too large ({mb} MB). "
        f"Telegram allows bots to send up to {limit_mb} MB."
    )
