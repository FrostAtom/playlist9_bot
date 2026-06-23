# 🎵 Music Downloader Bot

A Telegram bot that searches and downloads music from multiple sources and sends
it as a tagged MP3 (up to 320 kbps, with cover art and metadata). Built with
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
  `soundcloud.com`. **Spotify** and **Apple Music** track links are also
  accepted: the bot reads the artist + title and finds a match on YouTube Music
  (those platforms are DRM-protected and can't be downloaded directly).
- **Best available quality** — fetches the best source audio and sends MP3 at up
  to **320 kbps**.
- **Persistent cache** — delivered tracks are remembered in **PostgreSQL**
  (`file_id`), so re-requesting one (or sending it inline) is instant after a
  restart, with no re-download.
- **Paginated results** — clean "Artist — Title" buttons, 10 per page; the ◀/▶
  arrows appear only when there is somewhere to go.
- **Clean metadata** — MP3 files get tags (title, artist, album) and cover art
  embedded via `mutagen`, plus a dedicated 320×320 thumbnail for Telegram's
  preview. Missing album/cover are filled from **MusicBrainz + Cover Art
  Archive** (free, keyless; chosen after testing — it covers Russian music
  better than TheAudioDB/Discogs).
- **Concurrency & rate limits** — up to 3 simultaneous downloads per user and 8
  in total (extra requests are queued with a notice), plus a cap of 10 downloads
  per user per minute.
- **Cookies support** — point yt-dlp at a `cookies.txt` for age-restricted or
  region-locked content (see below).
- **Resilient downloads** — transient network/extractor failures are retried
  with backoff before giving up.
- **Ephemeral chat** — your query message is deleted immediately and the
  search-results message auto-deletes after a few minutes; only the delivered
  tracks stay.
- **Healthcheck** (event-loop heartbeat) and **graceful shutdown** on SIGTERM.

## 🚀 Quick start

### Run the published image (no clone needed)

Grab the compose file, drop your token into a `.env` next to it, and pull the
prebuilt image from GHCR:

```sh
curl -O https://raw.githubusercontent.com/FrostAtom/playlist9_bot/main/docker-compose.yml
echo "TELEGRAM_BOT_TOKEN=123456:ABC-DEF..." > .env   # only the token is required
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
cp .env.example .env          # then edit .env → TELEGRAM_BOT_TOKEN=...
docker compose up -d --build
```

## ⚙️ Configuration

Configuration is read from a local **`.env`** file (gitignored, so secrets never
land in git). Copy the template and fill it in — only `TELEGRAM_BOT_TOKEN` is
required (get one from [@BotFather](https://t.me/BotFather)); leave anything else
blank to use its default.

```sh
cp .env.example .env
# edit .env → TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
```

Compose reads `.env` automatically and injects the values into the container.

| Variable | Default | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | **Required.** Bot token from @BotFather. |
| `STORAGE_CHAT_ID` | — | Channel id (`-100…`) where the bot is admin; enables inline file delivery. |
| `DATABASE_URL` | — | PostgreSQL DSN for the persistent `file_id` cache. Computed from the bundled `db` service in compose; leave empty to run memory-only. |
| `POSTGRES_PASSWORD` | `playlist9` | Password for the bundled PostgreSQL; `DATABASE_URL` reuses it. |
| `MAX_FILE_SIZE_MB` | `50` | Max size of a sent file (Telegram caps bots at 50 MB). |
| `MAX_RESULTS` | `30` | Results fetched per search. |
| `RESULTS_PER_PAGE` | `10` | Results shown per page. |
| `AUDIO_QUALITY` | `320` | MP3 quality in kbps (best source, up to 320). |
| `INLINE_RESULTS` | `20` | Results fetched for an inline query. |
| `RATE_PER_MINUTE` | `10` | Max downloads a single user may trigger per minute. |
| `COOKIES_FILE` | — | Path to a `cookies.txt` inside the container; see [Cookies](#-cookies-age-restricted--region-locked-content). |

## 💬 Usage

1. Open the bot, send `/start`.
2. Send a track name, or paste a link (YouTube, SoundCloud, Spotify, Apple Music).
3. Get an MP3 back. Your query and the search results clean themselves up; only
   the delivered tracks stay.

### Inline mode setup

To send actual files inline, do the one-time setup:

1. In [@BotFather](https://t.me/BotFather): `/setinline` → pick the bot → set a
   placeholder, and `/setinlinefeedback` → enable (so the bot is told which
   result was chosen and can deliver the file).
2. Create a private channel, add the bot as an admin, and set `STORAGE_CHAT_ID`
   to its id (`-100…`). The bot uploads downloads there to obtain a `file_id`.

Without `STORAGE_CHAT_ID`, inline mode still re-sends already-cached tracks as
files and falls back to a link for new ones.

## 🍪 Cookies (age-restricted / region-locked content)

Some tracks won't download without a logged-in session (age-gated videos,
region-locked or "sign in to confirm" content). You can hand yt-dlp your browser
cookies to get past that:

1. Install a cookies exporter extension — e.g. **"Get cookies.txt LOCALLY"**
   ([Chrome](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc))
   — it exports in the Netscape `cookies.txt` format yt-dlp expects.
2. Log in to YouTube in your browser, open the exporter **on a `youtube.com`
   tab**, and export. Save the file as `cookies.txt`.
3. Drop it into the `cookies/` folder next to `docker-compose.yml` (it's mounted
   into the container at `/cookies`), and set in the bot's `environment:`:

   ```yaml
   COOKIES_FILE: /cookies/cookies.txt
   ```

4. `docker compose up -d`. If the path is empty or the file is missing, the bot
   simply runs without cookies.

> Treat `cookies.txt` like a password — it grants access to your account. Keep
> it private; it's already covered by `.gitignore`.

## 🧠 How it works

- **Search** goes through YouTube Music (`ytmusicapi`, `songs` filter) or
  SoundCloud (`yt-dlp` `scsearch`), one source at a time, switchable via the ⇄
  button.
- **Download** always pulls the audio with `yt-dlp` + `ffmpeg` and converts to
  MP3 (up to 320 kbps). YouTube Music supplies authoritative metadata; gaps are
  enriched from MusicBrainz.
- **Spotify / Apple Music** links can't be downloaded directly (DRM), so the bot
  reads the track's artist + title from the page's Open Graph tags and searches
  YouTube Music for a match — no API keys required.
- **Delivered `file_id`s** are stored in PostgreSQL, so the same track re-sends
  instantly later (and inline mode serves it as playable audio) without a
  re-download.

## 🏗️ Architecture

```
bot.py                      — thin entry point
app/
  config.py                 — settings from environment (Settings)
  models.py                 — domain models (Track, Meta, AudioFile)
  metadata.py               — filename cleanup, ID3 tags, cover + thumbnail
  metadata_provider.py      — metadata enrichment via MusicBrainz / Cover Art Archive
  external_links.py         — Spotify / Apple Music link → search query
  store.py                  — PostgreSQL-backed file_id cache (asyncpg)
  limiter.py                — concurrent download limits + per-user rate limit
  health.py                 — heartbeat for the healthcheck
  formatting.py             — results message + inline keyboard
  messages.py               — all user-facing text in one place
  sources/base.py           — AudioSource abstraction (extension seam)
  sources/ytdlp_source.py   — shared yt-dlp base class (download, retries, cookies)
  sources/youtube.py        — YouTube Music (search via ytmusicapi)
  sources/soundcloud.py     — SoundCloud (scsearch)
  service.py                — MusicService: search + download routing
  caches.py                 — in-memory state (recent searches, inline tracks)
  deps.py                   — Deps: the dependency bundle handlers close over
  delivery.py               — download → validate → send pipeline
  tg_utils.py               — error-tolerant aiogram call wrappers
  handlers.py               — aiogram router (thin: parse updates, dispatch)
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
- [**asyncpg**](https://github.com/MagicStack/asyncpg) `0.30.0` — PostgreSQL driver (persistent `file_id` cache)
- [**ffmpeg**](https://ffmpeg.org/) — audio extraction/conversion (system package)
- [**PostgreSQL**](https://www.postgresql.org/) `16` — persistent cache store (Docker service)
- Metadata: [MusicBrainz](https://musicbrainz.org/) + [Cover Art Archive](https://coverartarchive.org/)

## 👤 Author

Created by **Claude** (Anthropic's Claude Code).

## 📄 License

Released under the [MIT License](LICENSE).
