# 🎵 Music Downloader Bot

A Telegram bot that searches and downloads music from multiple sources and sends
it as a tagged MP3 (192 kbps, with cover art and metadata). Built with
[aiogram](https://github.com/aiogram/aiogram) 3, powered by `yt-dlp` + `ffmpeg`,
and shipped as a Docker image.

[![Telegram](https://img.shields.io/badge/Telegram-%40atomsdungeon__bot-26A5E4?logo=telegram&logoColor=white)](https://t.me/atomsdungeon_bot)
[![Python](https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3-2CA5E0)](https://github.com/aiogram/aiogram)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## ▶️ Try it

The bot runs live at **[@atomsdungeon_bot](https://t.me/atomsdungeon_bot)** —
open the chat, send a track name, and pick a result.

## ✨ Features

- **Single-source search with a toggle** — defaults to YouTube Music 🎵; the ⇄
  button switches the search to SoundCloud ☁️. Music only, no random videos.
- **Inline mode** — type `@atomsdungeon_bot query` in any chat to search and
  send the **track as a file**: cached tracks are sent instantly, new ones are
  downloaded on selection and the placeholder is replaced with the audio
  (requires `STORAGE_CHAT_ID`, see below).
- **Download by link** — `youtube.com`, `youtu.be`, `music.youtube.com`,
  `soundcloud.com`.
- **Paginated results** — "Artist — Title" buttons, 10 per page; the ◀/▶ arrows
  appear only when there is somewhere to go.
- **Clean metadata** — MP3 files get tags (title, artist, album) and cover art
  embedded via `mutagen`, plus a dedicated 320×320 thumbnail for Telegram's
  preview. Missing album/cover are filled from **MusicBrainz + Cover Art
  Archive** (free, keyless; chosen after testing — it covers Russian music
  better than TheAudioDB/Discogs).
- **Concurrency limits** — up to 3 simultaneous downloads per user and 8 in
  total; requests beyond that are queued with a notice.
- **Responsive** — downloads never block the bot; you can flip pages and queue
  more tracks while one is downloading.
- **/clear** — removes every bot message and query from the chat, keeping only
  the delivered tracks.
- **Healthcheck** (event-loop heartbeat) and **graceful shutdown** on SIGTERM.

## 🚀 Quick start

### Run the published image (no clone needed)

Grab just the compose file, create a `.env`, and pull the prebuilt image from
GHCR:

```sh
curl -O https://raw.githubusercontent.com/FrostAtom/playlist9_bot/main/docker-compose.yml
printf 'TELEGRAM_BOT_TOKEN=123456:ABC-DEF...\n' > .env   # see Configuration
docker compose pull
docker compose up -d
docker compose logs -f
```

The image is published to `ghcr.io/frostatom/playlist9_bot:latest` by CI on
every push to `main`.

### Build from source (for development)

```sh
git clone https://github.com/FrostAtom/playlist9_bot.git
cd playlist9_bot
cp .env.example .env        # then put your bot token in .env
docker compose up -d --build
```

## ⚙️ Configuration

All configuration is via environment variables — see **[`.env.example`](.env.example)**
for the full list with defaults. The only required value is `TELEGRAM_BOT_TOKEN`
(get one from [@BotFather](https://t.me/BotFather)).

## 💬 Usage

1. Open the bot, send `/start`.
2. Send a track name, or paste a link.
3. Get an MP3 back. `/clear` wipes the chat of everything except tracks.

### Inline mode setup

To send actual files inline, do the one-time setup:

1. In [@BotFather](https://t.me/BotFather): `/setinline` → pick the bot → set a
   placeholder, and `/setinlinefeedback` → enable (so the bot is told which
   result was chosen and can deliver the file).
2. Create a private channel, add the bot as an admin, and set `STORAGE_CHAT_ID`
   to its id (`-100…`). The bot uploads downloads there to obtain a `file_id`.

Without `STORAGE_CHAT_ID`, inline mode still re-sends already-cached tracks as
files and falls back to a link for new ones.

## 🧠 How it works

- **Search** goes through YouTube Music (`ytmusicapi`, `songs` filter) or
  SoundCloud (`yt-dlp` `scsearch`), one source at a time, switchable via the ⇄
  button.
- **Download** always pulls the audio with `yt-dlp` + `ffmpeg` and converts to
  MP3. YouTube Music supplies authoritative metadata; gaps are enriched from
  MusicBrainz.
- **Spotify** is intentionally not included — it requires paid API access and
  is DRM-protected.

## 🏗️ Architecture

```
bot.py                      — thin entry point
app/
  config.py                 — settings from environment (Settings)
  models.py                 — domain models (Track, Meta, AudioFile)
  metadata.py               — filename cleanup, ID3 tags, cover + thumbnail
  metadata_provider.py      — metadata enrichment via MusicBrainz / Cover Art Archive
  limiter.py                — concurrent download limits (per-user / total)
  health.py                 — heartbeat for the healthcheck
  formatting.py             — results message + inline keyboard
  messages.py               — all user-facing text in one place
  sources/base.py           — AudioSource abstraction (extension seam)
  sources/ytdlp_source.py   — shared yt-dlp base class
  sources/youtube.py        — YouTube Music (search via ytmusicapi)
  sources/soundcloud.py     — SoundCloud (scsearch)
  service.py                — MusicService: search + download routing
  handlers.py               — aiogram router (search, pagination, inline, download)
  application.py            — Dispatcher/Bot wiring, polling, graceful shutdown
healthcheck.py              — Docker HEALTHCHECK script (checks the heartbeat)
```

**Adding a source:** implement `AudioSource` (or subclass `YtDlpSource`) and
register it in `build_service()` (`app/application.py`).

## 🛠️ Tech stack & dependencies

- [**aiogram**](https://github.com/aiogram/aiogram) `3.15.0` — async Telegram Bot framework
- [**yt-dlp**](https://github.com/yt-dlp/yt-dlp) `2026.6.9` — audio downloading
- [**ytmusicapi**](https://github.com/sigma67/ytmusicapi) `1.12.1` — YouTube Music search (keyless)
- [**mutagen**](https://github.com/quodlibet/mutagen) `1.47.0` — ID3 tagging
- [**Pillow**](https://github.com/python-pillow/Pillow) `11.0.0` — thumbnail generation
- [**ffmpeg**](https://ffmpeg.org/) — audio extraction/conversion (system package)
- Metadata: [MusicBrainz](https://musicbrainz.org/) + [Cover Art Archive](https://coverartarchive.org/)

## 👤 Author

Created by **Claude** (Anthropic's Claude Code).

## 📄 License

Released under the [MIT License](LICENSE).
