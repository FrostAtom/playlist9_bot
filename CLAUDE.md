# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram music-download bot (aiogram 3, polling) that searches YouTube Music
and SoundCloud, downloads audio with `yt-dlp` + `ffmpeg`, tags it, and sends MP3s.
The same engine also backs a public **web download page** (a neon/cyberpunk
landing site with search + one-click MP3 download) and a status dashboard, each
on its own HTTP port. Runs only as a Docker container — there is no host venv.

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

### Tests

There is a pytest suite under `tests/` (offline + deterministic — no network,
no downloads). It runs **only in Docker** (no host venv); dev-only deps live in
`requirements-dev.txt` and are installed at runtime, not baked into the image:

```powershell
docker run --rm -v "${PWD}:/app" youtube-music-bot `
  sh -c "pip install --user -q -r requirements-dev.txt && python -m pytest"
```

The suite covers `app/music/resolver.py` (the shared input classifier) and the
pure web helpers, plus the `/api/search` dispatch against an offline stub
service. After testing, clean up any temp containers you created.

For ad-hoc end-to-end checks (real search/download), **write a script to a file
and copy it in** (or bind-mount the repo over `/app`), then run it — do NOT pass
multi-line Python via `docker exec ... -c` (PowerShell mangles inner double
quotes):

```powershell
docker cp test.py youtube-music-bot:/app/test.py
docker exec youtube-music-bot python /app/test.py
```

`build_service(Settings(token="x"))` gives a working `MusicService` offline (no
Telegram needed) for testing search/download/metadata in isolation.

## Architecture

Layered package `app/`, wired in `app/application.py` (`build_service` + `_amain`).
The layout groups files by concern — `music/` (the engine), `bot/` (Telegram
presentation), `infra/` (cross-cutting plumbing), `web/` (download page + status
dashboard):

```
app/
  config.py                  — Settings (env-only, frozen dataclass)
  application.py             — composition root: build_service + _amain + run
  models.py                  — shared domain models (Track, Meta, AudioFile)
  music/                     — search/download engine
    service.py               — MusicService facade (routes across sources)
    resolver.py              — shared input classifier (bot + inline + web)
    links.py                 — Spotify/Apple link & playlist/album scraping
    metadata.py              — filename cleanup, ID3 tags, cover + thumbnail
    metadata_provider.py     — album/cover enrichment (MusicBrainz / Cover Art Archive)
    video.py                 — TikTok link detect + video (MP4) download (yt-dlp)
    sources/
      base.py                — AudioSource abstraction (extension seam)
      ytdlp.py               — shared yt-dlp primitive `download_media` (cookies, retries, thumbnail) + audio source base
      youtube.py             — YouTube Music (search via ytmusicapi)
      soundcloud.py          — SoundCloud (scsearch)
  bot/                       — aiogram presentation layer
    router.py                — build_router: thin handlers + activity middleware
    delivery.py              — download → validate → send pipeline (audio + video)
    inline.py                — inline-mode: classify query/link → results → file_id
    formatting.py            — result/playlist messages + inline keyboards
    messages.py              — all user-facing text
    caches.py                — in-memory state (SearchCache, InlineCache, LinkCache)
    deps.py                  — Deps: the dependency bundle handlers close over
    telegram.py              — error-tolerant aiogram call wrappers
  infra/                     — cross-cutting infrastructure
    store.py                 — PostgreSQL file_id cache (asyncpg)
    limiter.py               — concurrent-download + per-user rate limits
    health.py                — heartbeat for the Docker healthcheck
    metrics.py               — counters, unique users, recent-log buffer
  web/                       — web surfaces (two aiohttp servers, two ports)
    server.py                — start_web_server (dashboard) + start_download_server (page)
    api.py                   — /api/search + /api/download handlers for the page
    templates/landing.html   — neon/cyberpunk music-download page markup
    templates/status.html    — the status dashboard markup
tests/                       — pytest suite (resolver, web helpers, /api/search)
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
- **Input classifier** (`music/resolver.py`) is the *single source of truth* for
  "what did the user paste/type?" — TikTok post, YouTube/SoundCloud track /
  playlist / ambiguous link, Spotify/Apple track / playlist / album, or a plain
  search. `classify(service, text)` is **pure and synchronous** (regex/URL
  matching only — no network) and returns a `ClassifiedInput` (kind + extracted
  URLs). All three free-form entry points branch on it: `bot/router.py::on_text`,
  `bot/inline.py::_resolve_tracks`, and `web/api.py::search` — they then do their
  own side effects (the bot auto-downloads & sends; the web returns a track
  list). `external_items_to_tracks` is the shared Spotify/Apple-collection →
  query-backed `Track` converter. Covered by `tests/test_resolver.py`. When you
  change the classification, change it here — not in the callers.
- **TikTok video** (`music/video.py`) is a deliberately *separate* path from the
  audio engine: a TikTok link is a video, so extracting MP3 would be wrong.
  `detect_tiktok` recognizes the link in `on_text` (before search), and
  `VideoDownloader.download` fetches the original clip as a single MP4 via the
  same shared `download_media` primitive the audio sources use (so cookies,
  retries and thumbnail handling are unified) — it just passes a video `format`
  and no audio postprocessing. It's delivered by `bot/delivery.deliver_video`
  (`send_video`, not `send_audio`) and never touches `MusicService`,
  `FileIdStore`, or the ID3/metadata pipeline. `VideoDownloader` is held on
  `Deps.video` and built in `build_service`'s caller (`_amain`).
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
- **Web download page** (`web/server.py::start_download_server` + `web/api.py`)
  is a public, audio-only mirror of the bot, served on its own port (`WEB_PORT`,
  default 8080). It needs **no Telegram token and no DB** — just a `MusicService`
  plus the limiters — so it can be hosted standalone. `/api/search` classifies
  the query via `music/resolver.py` (same tree as the bot, minus TikTok) and
  returns `Track`s; `/api/download` downloads the tagged MP3 and streams it back
  as an attachment (RFC-5987 filename, no Telegram `file_id` round-trip).
  Downloads are gated by the same `DownloadLimiter`/`RateLimiter`, keyed by client
  IP. The page itself is `templates/landing.html` (neon/cyberpunk, single
  self-contained file; the source list is spliced into its JS at render).
- **Status dashboard** (`web/server.py::start_web_server`) serves a live metrics
  + error-log page on `METRICS_PORT` (default 8473), fed by `infra/metrics.py`
  (incremented across `bot/` *and* `web/api.py`) and a yt-dlp staleness check.

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
- **Inline mode** (`bot/inline.py`) accepts the same inputs as `on_text`: a plain
  search *or* a pasted link — a YouTube/SoundCloud track or playlist, a
  Spotify/Apple track or playlist/album, or a TikTok clip. `_resolve_tracks`
  classifies the query (mirroring `on_text`) and collapses everything except
  TikTok to a list of `Track`s (a single pasted track URL goes through
  `MusicService.resolve_track`, a metadata-only yt-dlp extract); TikTok is its
  own video result. Inline can only send a file via a `file_id`, which can't be
  produced during the inline query. So: cached tracks (`FileIdStore`, keyed
  `source:id`, populated on every successful send) are returned as
  `InlineQueryResultCachedAudio` immediately; everything else returns an article
  placeholder whose payload (`InlineRef`: a `Track` or a TikTok URL) is stashed in
  `InlineCache` keyed by the result id, and `chosen_inline_result` downloads →
  uploads to `STORAGE_CHAT_ID` to mint a `file_id` → `edit_message_media` swaps
  the placeholder for audio (`ensure_file_id`) or video (`ensure_video_file_id`).
  Result ids double as the cache key: real tracks use the stable `source:id`;
  synthetic items (Spotify/Apple playlist picks, which have no direct URL — they
  carry a `query` resolved at download time) and TikTok URLs use a content hash,
  so they stay unique within an answer and aren't persistently cached (a
  query-track's id would otherwise collide across playlists). This requires, in
  @BotFather, **`/setinline`** and **`/setinlinefeedback` enabled** (without
  feedback no `chosen_inline_result` arrives), plus the bot added as **admin to
  the storage chat** (startup logs a warning if it's unreachable). Video file_ids
  aren't persisted — TikTok links are one-off, so the storage round-trip repeats.

## Gotchas

- **Keep `yt-dlp` current.** YouTube periodically breaks older versions
  (symptoms: "No video formats found"). Bump the pin in `requirements.txt`.
  `download_media` (the shared yt-dlp primitive in `sources/ytdlp.py`) retries
  transient failures with backoff but won't fix a stale yt-dlp (errors matching
  `_PERMANENT_ERROR` aren't retried).
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
