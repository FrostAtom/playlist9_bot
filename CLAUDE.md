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
The layout groups files by concern — `music/` (the engine), `bot/` (Telegram
presentation), `infra/` (cross-cutting plumbing), `web/` (status dashboard):

```
app/
  config.py                  — Settings (env-only, frozen dataclass)
  application.py             — composition root: build_service + _amain + run
  models.py                  — shared domain models (Track, Meta, AudioFile)
  music/                     — search/download engine
    service.py               — MusicService facade (routes across sources)
    links.py                 — Spotify/Apple link & playlist/album scraping
    metadata.py              — filename cleanup, ID3 tags, cover + thumbnail
    metadata_provider.py     — album/cover enrichment (MusicBrainz / Cover Art Archive)
    sources/
      base.py                — AudioSource abstraction (extension seam)
      ytdlp.py               — shared yt-dlp base (download, retries, playlists, cookies)
      youtube.py             — YouTube Music (search via ytmusicapi)
      soundcloud.py          — SoundCloud (scsearch)
  bot/                       — aiogram presentation layer
    router.py                — build_router: thin handlers + activity middleware
    delivery.py              — download → validate → send pipeline
    formatting.py            — result/playlist messages + inline keyboards
    messages.py              — all user-facing text
    caches.py                — in-memory state (SearchCache, TrackCache, LinkCache)
    deps.py                  — Deps: the dependency bundle handlers close over
    telegram.py              — error-tolerant aiogram call wrappers
  infra/                     — cross-cutting infrastructure
    store.py                 — PostgreSQL file_id cache (asyncpg)
    limiter.py               — concurrent-download + per-user rate limits
    health.py                — heartbeat for the Docker healthcheck
    metrics.py               — counters, unique users, recent-log buffer
  web/                       — status dashboard
    server.py                — aiohttp server; / + /api/stats + /healthz
    templates/status.html    — the page markup
```

- **Sources** (`app/music/sources/`) implement `AudioSource` (`base.py`):
  `handles` (is this URL mine?), `search`, `download`, plus `track_url` /
  `playlist_url` (link classification) and `list_playlist`. `YtDlpSource` is the
  shared base doing the yt-dlp download and flat playlist enumeration.
  `YouTubeMusicSource` overrides `search` to use `ytmusicapi` (keyless, `songs`
  filter); `SoundCloudSource` uses yt-dlp `scsearch`. Add a platform by
  subclassing and registering it in `build_service`.
- **`MusicService`** (`music/service.py`) is the facade: `search(query, limit,
  source)` hits one source; `download(ref)` accepts a `Track` or a raw URL;
  `resolve`/`link_info` classify a pasted URL; `playlist` enumerates one.
- **External links** (`music/links.py`) recognize Spotify / Apple Music track,
  playlist and album URLs and scrape artist+title (keyless). For a single track
  `on_text` searches YouTube Music and auto-downloads the top match; playlists/
  albums become a paginated pick list (each item searched on demand).
- **Persistence** (`infra/store.py`): `FileIdStore` caches `source:id → file_id`
  in PostgreSQL (asyncpg) behind an in-memory LRU. The connection is built from
  discrete `DATABASE_HOST/PORT/USER/PASSWORD/NAME` settings (no DSN string to
  URL-escape) and is mandatory — `create_pool` retries at startup and exits
  (→ container restart) if the DB never becomes reachable. `get`/`get_many`/`put`
  are async, so their callers (`bot/delivery.py`, `bot/router.py`) await.
- **Metadata pipeline** is the subtle part. `sources` call into
  `music/metadata.py`: `finalize_with_metadata` (search picks — clean tags known)
  or `finalize_download` (pasted URLs — clean the messy yt-dlp title). Both embed
  ID3 tags + cover with mutagen and generate a 320×320 JPEG thumbnail (Pillow).
  `music/metadata_provider.py` enriches missing album/cover via MusicBrainz +
  Cover Art Archive (only when album or cover is absent).
- **Bot** (`bot/router.py`) is a deliberately thin aiogram `Router` built by
  `build_router` — it parses updates and decides *what* to do. The *how* is split
  out: the download→validate→send pipeline in `bot/delivery.py`, in-memory state
  in `bot/caches.py`, the `Deps` bundle in `bot/deps.py`, and error-tolerant
  aiogram wrappers in `bot/telegram.py`. All bot text lives in `bot/messages.py`
  (English); keyboards in `bot/formatting.py`.
- **Status dashboard** (`web/server.py`) serves a live metrics + error-log page,
  fed by `infra/metrics.py` (incremented across `bot/`) and a yt-dlp staleness
  check.

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
- Config is env-only, set as literal values in the compose `environment:` blocks
  (`config.py`, frozen `Settings`) — no `${...}` interpolation and no `.env`, so
  the stack imports cleanly on CasaOS/Portainer. Empty/blank values fall back to
  defaults via `_env_int` / `_env_str`, so unset fields never crash startup.
  Notable: `AUDIO_QUALITY` defaults to `320`; the Postgres connection is discrete
  (`DATABASE_HOST/PORT/USER/PASSWORD/NAME`, defaulting to the bundled `db`
  service); `METRICS_PORT` (status page) defaults to `8473`; `COOKIES_FILE`
  (yt-dlp cookies, only used if the path exists) and `RATE_PER_MINUTE` are
  optional.
- Compose runs **two services** (`bot` + `db`), wired by literal matching creds
  (`DATABASE_*` on the bot must equal the `db` service's `POSTGRES_*`). The DB is
  mandatory at runtime — the bot exits and restarts until Postgres is reachable.
  For a quick one-off container (the `docker run` iterate path), the discrete
  `DATABASE_*` defaults point at host `db`, so set `DATABASE_HOST` or expect the
  startup DB-connect to fail.
