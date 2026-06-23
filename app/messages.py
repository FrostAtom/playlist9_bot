"""User-facing text. Centralized so wording/localization lives in one place."""
from __future__ import annotations

WELCOME = (
    "🎵 *Music Downloader*\n\n"
    "• Send a *track name* — I'll search YouTube Music 🎵 (tap ⇄ to switch the "
    "search to SoundCloud ☁️) and let you pick.\n"
    "• Or send a *link* — YouTube/SoundCloud download directly; Spotify & Apple "
    "Music links are matched on YouTube Music.\n"
    "• In any chat, type `@bot_name query` for inline search.\n\n"
    "Files arrive at up to 320 kbps with cover art and clean tags."
)

DOWNLOADING = "⏳ Downloading audio, please wait..."
DOWNLOADING_CHOICE = "⏳ Downloading the selected track, please wait..."
RESOLVING_LINK = "🔗 Reading the link..."
QUEUED = "⏳ Queued — the concurrent download limit has been reached..."
UPLOADING = "📤 Uploading the file..."
NO_AUDIO = "❌ Could not extract audio from the link."
NOT_FOUND = "Nothing found 😔 Try a different query."
LINK_FAILED = "❌ Couldn't read that link. Try sending the track name instead."
RESULTS_EXPIRED = "Results expired, please search again 🔁"
SEARCH_ERROR = "Search error, please try again 🔁"


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
