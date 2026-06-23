# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram music-download bot (aiogram 3, polling) that searches YouTube Music
and SoundCloud, downloads audio with `yt-dlp` + `ffmpeg`, tags it, and sends MP3s.
Runs only as a Docker container — there is no host venv and no test suite.

## Commands

```sh
# Build + run (production-ish)
docker compose up -d --build
docker compose logs -f

# Iterate on a change
docker rm -f youtube-music-bot
docker build -t youtube-music-bot .
docker run -d --name youtube-music-bot -e TELEGRAM_BOT_TOKEN=... youtube-music-bot
docker logs youtube-music-bot --tail 20

# Health / shutdown (compose names the container `playlist9_bot`)
docker inspect --format '{{.State.Health.Status}}' playlist9_bot
docker compose down                  # graceful SIGTERM, exits 0 in ~1s
```

`docker-compose.yml` pulls the published image `ghcr.io/frostatom/playlist9_bot`
(built/pushed multi-arch — `linux/amd64,linux/arm64` — by
`.github/workflows/docker-publish.yml`) but also has `build: .`, so from a clone
`docker compose up -d --build` builds locally instead (the path for Raspberry
Pi / ARM). The Dockerfile is arch-agnostic and verified to build on arm64.

There are no automated tests. To exercise code, **write a script to a file and
copy it in**, then run it — do NOT pass multi-line Python via `docker exec ... -c`
(PowerShell mangles inner double quotes):

```powershell
docker cp test.py youtube-music-bot:/app/test.py
docker exec youtube-music-bot python /app/test.py
```

`build_service(Settings(token="x"))` gives a working `MusicService` offline (no
Telegram needed) for testing search/download/metadata in isolation.

## Architecture

Layered package `app/`, wired in `app/application.py` (`build_service` + `_amain`).

- **Sources** (`app/sources/`) implement `AudioSource` (`base.py`): `handles`
  (is this URL mine?), `search`, `download`. `YtDlpSource` is the shared base
  doing the actual yt-dlp download. `YouTubeMusicSource` overrides `search` to
  use `ytmusicapi` (keyless, `songs` filter — clean metadata, no random videos);
  `SoundCloudSource` uses yt-dlp `scsearch`. Add a platform by subclassing and
  registering it in `build_service`.
- **`MusicService`** (`service.py`) is the facade: `search(query, limit, source)`
  hits one source; `download(ref)` accepts a `Track` (carries authoritative
  metadata → `Meta`) or a raw URL (metadata derived from the file). `resolve`
  maps a pasted URL to its source.
- **External links** (`external_links.py`) recognize Spotify / Apple Music track
  URLs and scrape artist+title from Open Graph tags (keyless). `on_text` then
  searches YouTube Music and auto-downloads the top match (those platforms are
  DRM-protected — no direct download).
- **Persistence** (`store.py`): `FileIdStore` caches `source:id → file_id` in
  PostgreSQL (asyncpg) behind an in-memory LRU. Optional — with no `DATABASE_URL`
  (or the DB unreachable, retried at startup) it degrades to memory-only.
  `get`/`get_many`/`put` are async, so their callers (`delivery.py`,
  `handlers.py`) await.
- **Metadata pipeline** is the subtle part. `sources` call into `metadata.py`:
  `finalize_with_metadata` (search picks — clean tags known) or
  `finalize_download` (pasted URLs — clean the messy yt-dlp title). Both embed
  ID3 tags + cover with mutagen and generate a 320×320 JPEG thumbnail (Pillow)
  for Telegram's audio preview. `metadata_provider.py` enriches missing
  album/cover via MusicBrainz + Cover Art Archive (chosen for free, keyless,
  decent Russian coverage; only called when album or cover is absent).
- **Handlers** (`handlers.py`) are a deliberately thin aiogram `Router` built by
  `build_router` — they parse updates and decide *what* to do. The *how* is split
  out: the download→validate→send pipeline in `delivery.py`, in-memory state
  (`SearchCache`/`TrackCache`) in `caches.py`, the `Deps` bundle (settings,
  service, `DownloadLimiter`, `RateLimiter`, caches, Postgres-backed
  `FileIdStore`) in `deps.py`, and error-tolerant aiogram wrappers in
  `tg_utils.py`. All bot text lives in `messages.py` (English); keyboards in
  `formatting.py`.

## Cross-cutting mechanics (read before touching handlers)

- **Search state & tokens.** `callback_data` is capped at 64 bytes, so a search
  is keyed by its *results message id* (the token). `SearchCache` stores
  `SearchState(query, source, tracks)` per user; pagination/toggle/pick all look
  up by token. The toggle button (`formatting.source_short`) re-runs the query on
  the other source; arrows are omitted at the first/last page.
- **Concurrency & rate limit.** `DownloadLimiter` (per-user 3, total 8, asyncio
  semaphores) wraps every download; `busy()` decides whether to show a "queued"
  notice. aiogram processes updates as tasks, so downloads never block
  pagination. A separate `RateLimiter` (sliding window, default 10/user/min)
  gates every download entry point — `on_pick`, the URL/Spotify branch of
  `on_text`, and inline `delivery.ensure_file_id`.
- **Ephemeral chat.** There is no `/clear`. Instead `on_text` deletes the user's
  query message immediately (`_safe_delete`), and the search-results message
  auto-deletes after `RESULTS_TTL` (5 min) via `_delete_after` (a fire-and-forget
  `asyncio.create_task`). Delivered audio is never deleted. Inline mode deletes
  nothing — it only searches and sends.
- **Inline mode** can only send a file via a `file_id`, which can't be produced
  during the inline query. So: cached tracks (`FileIdStore`, keyed
  `source:id`, populated on every successful send) are returned as
  `InlineQueryResultCachedAudio` immediately; uncached tracks return an article
  placeholder, and `chosen_inline_result` downloads → uploads to
  `STORAGE_CHAT_ID` to mint a `file_id` → `edit_message_media` swaps the
  placeholder for audio. This requires, in @BotFather, **`/setinline`** and
  **`/setinlinefeedback` enabled** (without feedback no `chosen_inline_result`
  arrives), plus the bot added as **admin to the storage chat** (startup logs a
  warning if it's unreachable).

## Gotchas

- **Keep `yt-dlp` current.** YouTube periodically breaks older versions
  (symptoms: "No video formats found"). Bump the pin in `requirements.txt`.
  `_extract_with_retry` retries transient failures with backoff but won't fix a
  stale yt-dlp (errors matching `_PERMANENT_ERROR` aren't retried).
- aiogram's `send_audio` `duration` must be `int`; SoundCloud reports floats.
  `metadata._finalize` already coerces — keep it that way.
- The bot's `@username` (used for inline attribution links) is resolved at
  startup via `bot.get_me()` and stored on `Deps.bot_username` — never hardcode
  it.
- Config is env-only, set in the compose `environment:` block (`config.py`,
  frozen `Settings`). Empty/blank values fall back to defaults via `_env_int` /
  `_env_str`, so unset fields never crash startup. Notable: `AUDIO_QUALITY`
  defaults to `320`; `DATABASE_URL` (Postgres, preset to the compose `db`
  service), `COOKIES_FILE` (yt-dlp cookies, only used if the path exists), and
  `RATE_PER_MINUTE` are all optional.
- Compose now runs **two services** (`bot` + `db`). The `db` is required for
  persistence but the bot still starts without it (memory-only). For a quick
  one-off container (the `docker run` iterate path) just omit `DATABASE_URL`.
