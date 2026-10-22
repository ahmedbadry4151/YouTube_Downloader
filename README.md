# YouTube_Downloader

# TurboTube

A fast, self-hosted YouTube downloader with a zero-build web UI, a scriptable CLI, and a parallel download engine built on **yt-dlp**, **aria2c**, and **ffmpeg**.

TurboTube is designed for one thing: saturating your connection while keeping full control over quality, playlists, subtitles, and output naming.

```
┌─────────────────────────────────────────────────────────────┐
│  Browser (vanilla HTML/CSS/JS)        CLI (rich terminal)   │
│         │  SSE progress                       │             │
│         └──────────────┬───────────────────────┘             │
│                  FastAPI + Uvicorn                           │
│                        │                                     │
│              Job manager (thread pool)                       │
│         ┌──────────────┼──────────────┐                      │
│      aria2c        yt-dlp core      ffmpeg                   │
│         └──────────────┴──────────────┘                      │
│                  SQLite (WAL) + TT_* config                  │
└─────────────────────────────────────────────────────────────┘
```

---

## Features

- **Quality control** — best, 2160p, 1440p, 1080p, 720p, 480p, 360p, worst, or audio-only (MP3/M4A/Opus/AAC/FLAC/WAV).
- **Playlists** — expand a playlist, preview every entry, and download only the videos you pick (`1-5,8,10-12`) or tick in the UI.
- **Real parallelism** — multiple videos at once *plus* multiple connections per video (aria2c or yt-dlp fragment concurrency).
- **Live progress** — Server-Sent Events stream per-item speed, ETA, size, and state; no polling, no page refresh.
- **Durable queue** — jobs survive a restart (SQLite WAL); interrupted jobs are marked and can be re-queued.
- **Per-item control** — retry or cancel a single video without touching the rest of the playlist.
- **Post-processing** — container remux (MP4/MKV/WebM), subtitle download and embedding, thumbnail embedding, metadata, chapters, SponsorBlock mark/remove.
- **Advanced input** — cookies from file or browser, proxies, rate limits, time sections (`00:30-02:10`), output templates, download archive to skip duplicates.
- **Security-first** — SSRF protection, private-IP blocking, optional API key, CSP, path containment, and no shell interpolation.
- **Two interfaces, one engine** — the same options and behaviour in the Web UI, REST API, and CLI.

---

## Requirements

| Dependency | Version | Notes |
| --- | --- | --- |
| Python | 3.11+ | 3.13 recommended |
| ffmpeg + ffprobe | any recent | required for merging, remuxing, audio extraction |
| aria2c | 1.36+ | optional but strongly recommended for maximum bandwidth |
| Node.js / Deno / Bun | Node 20+ | solves YouTube's JavaScript challenges; without one, downloads may fail with HTTP 403 |

The `yt-dlp-ejs` challenge-solver package is installed automatically with the Python dependencies. TurboTube auto-detects the first available runtime (`deno`, `node`, `bun`, then `quickjs`).

Check everything at once:

```powershell
python -m app doctor
```

Install the external tools:

```powershell
# Windows (winget)
winget install Gyan.FFmpeg
winget install aria2.aria2
winget install OpenJS.NodeJS.LTS

# macOS
brew install ffmpeg aria2 node

# Debian / Ubuntu
sudo apt update && sudo apt install -y ffmpeg aria2 nodejs
```

---

## Quick start

```powershell
# 1. Install Python dependencies
python -m pip install -r requirements.txt

# 2. Optional: create your config
Copy-Item .env.example .env

# 3. Verify the toolchain
python -m app doctor

# 4. Start the web UI
python -m app serve
```

Open <http://127.0.0.1:8000>. On Windows you can also double-click **`start.bat`**.

To expose the server on your LAN (read the [security](#security) section first):

```powershell
python -m app serve --host 0.0.0.0 --port 8000
```

---

## Web UI

1. Paste a video or playlist URL and press **Analyze**.
2. For a playlist, tick the videos you want or type a range in the selection box; use **All / None / Invert** for bulk changes.
3. Pick a quality chip (or switch to **Audio only**) and adjust container and extras under **Advanced**.
4. Press **Download**. The queue shows live speed, ETA, and progress per video, with per-item cancel/retry.

The UI stores your last-used options in the browser and keeps the queue in sync through SSE, so you can reload the tab without losing state.

---

## CLI

```powershell
python -m app --help
python -m app download --help
```

### Download a single video

```powershell
# Best video + audio, merged to MP4
python -m app download "https://www.youtube.com/watch?v=VIDEO_ID"

# 1080p MKV
python -m app download "https://youtu.be/VIDEO_ID" -q 1080 --container mkv

# Audio only, MP3 (best VBR quality)
python -m app download "https://youtu.be/VIDEO_ID" --audio mp3

# FLAC audio
python -m app download "https://youtu.be/VIDEO_ID" --audio flac

# Only a section of the video
python -m app download "https://youtu.be/VIDEO_ID" --section 00:30-02:10
# Time-range (section) downloads always use the native downloader for a
# precise cut; aria2c is not used for byte-range slices.

# Custom output folder, 8 parallel videos, 32 fragments each
python -m app download "https://youtu.be/VIDEO_ID" -o D:\Media -w 8 --fragments 32
```

### Playlists and partial downloads

```powershell
# First five videos
python -m app download "https://www.youtube.com/playlist?list=PL..." --items 1-5

# Hand-picked episodes
python -m app download "https://www.youtube.com/playlist?list=PL..." --items "1-5,8,10-12"

# Reverse playlist order, everything
python -m app download "https://www.youtube.com/playlist?list=PL..." --reverse

# Preview the selection without downloading
python -m app download "https://www.youtube.com/playlist?list=PL..." --items 1-3 --dry-run
```

Selection syntax supports single indices, inclusive ranges (`3-7`), open-ended ranges (`5-` means "from 5 to the end"), and comma or whitespace separators. Ranges must be forward and within the playlist length; invalid selections exit with a clear error.

### Analysis and automation

```powershell
# Inspect a URL (playlist entries, available qualities)
python -m app probe "https://www.youtube.com/playlist?list=PL..."

# Machine-readable probe
python -m app probe "https://youtu.be/VIDEO_ID" --json

# Machine-readable download result
python -m app download "https://youtu.be/VIDEO_ID" --json
```

### Auth, cookies, and network options

```powershell
# Use a Netscape cookie file (age-restricted / members-only content)
python -m app download "URL" --cookies cookies.txt

# Pull cookies directly from a logged-in browser
python -m app download "URL" --cookies-from-browser chrome

# Route through a proxy and cap bandwidth at 8 MiB/s
python -m app download "URL" --proxy "http://127.0.0.1:8080" --limit-rate 8M

# Skip videos already recorded in the archive
python -m app download "URL" --archive
```

### Extras

```powershell
python -m app download "URL" `
  --subtitles --auto-subs --sub-langs "en,de" `
  --chapters --sponsorblock remove `
  --no-aria2
```

Run `python -m app` with no arguments to start the server (same as `serve`).

---

## Performance tuning

TurboTube has three independent concurrency layers. Raising all of them blindly causes throttling, so tune incrementally.

| Knob | Default | What it does |
| --- | --- | --- |
| `TT_MAX_CONCURRENT_DOWNLOADS` | `4` | Videos downloaded simultaneously. Main multiplier for playlists. |
| `TT_FRAGMENTS_PER_DOWNLOAD` | `16` | Parallel byte-range requests per video (DASH/HLS fragments). |
| `TT_ARIA2_CONNECTIONS` | `16` | Connections per video when aria2c is the transport. |
| `TT_HTTP_CHUNK_SIZE_MB` | `10` | Chunk size for yt-dlp's native downloader. |
| `TT_USE_ARIA2` | `true` | Use aria2c when it is on `PATH`. |
| `TT_RETRIES` | `3` | Retries for transient network errors. |
| `TT_SOCKET_TIMEOUT` | `20` | Socket timeout in seconds. |

Suggested starting points:

- **Single large video, fast connection:** `TT_FRAGMENTS_PER_DOWNLOAD=32`, `TT_ARIA2_CONNECTIONS=32`.
- **Large playlist:** `TT_MAX_CONCURRENT_DOWNLOADS=6` to `8`, fragments `8` to `16`. Keeping fragments lower per video avoids connection storms.
- **Weak Wi-Fi or unstable link:** workers `2`, fragments `8`, retries `5`, socket timeout `30`.
- **CPU-bound merging:** parallel workers compete for ffmpeg cores. On a 4-core machine keep workers at `2`–`3`.

Practical ceiling: `workers × connections` should stay well under your router's NAT table and your ISP's per-host limits. When throughput stops improving and errors rise, back off.

> Disk speed matters too. Merging 4K video to a slow HDD while downloading 8 streams is a common bottleneck; point `TT_DOWNLOAD_DIR` at an SSD for heavy use.

---

## Configuration

Every setting is a `TT_`-prefixed environment variable, read from the process environment or a `.env` file in the project root. Start from [`.env.example`](.env.example).

| Variable | Default | Description |
| --- | --- | --- |
| `TT_HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` for LAN access. |
| `TT_PORT` | `8000` | HTTP port. |
| `TT_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error`, `critical`. |
| `TT_API_KEY` | *(empty)* | When set, `/api/*` requires `X-API-Key`. |
| `TT_CORS_ORIGINS` | *(empty)* | Comma-separated allowed origins. |
| `TT_DOWNLOAD_DIR` | `downloads` | Output directory (relative to project root or absolute). |
| `TT_DATA_DIR` | `data` | Database, cookies, and logs. |
| `TT_MAX_CONCURRENT_DOWNLOADS` | `4` | Parallel videos (1–32). |
| `TT_FRAGMENTS_PER_DOWNLOAD` | `16` | Fragments per video (1–64). |
| `TT_HTTP_CHUNK_SIZE_MB` | `10` | Native downloader chunk size (1–64). |
| `TT_USE_ARIA2` | `true` | Prefer aria2c when available. |
| `TT_ARIA2_CONNECTIONS` | `16` | aria2c connections per video (1–32). |
| `TT_RETRIES` | `3` | Transient-failure retries (0–20). |
| `TT_SOCKET_TIMEOUT` | `20` | Socket timeout in seconds (5–120). |
| `TT_PROBE_CACHE_TTL` | `180` | URL analysis cache in seconds (`0` disables). |
| `TT_PROBE_MAX_ENTRIES` | `1000` | Playlist entries returned by `/api/probe`. |
| `TT_MAX_HISTORY` | `200` | Jobs kept in SQLite and memory. |
| `TT_ENABLE_OPEN_FOLDER` | `true` | Show the "open folder" action in the UI. |
| `TT_AUTO_RESUME` | `false` | Re-queue jobs interrupted by a restart. |
| `TT_ALLOWED_HOSTS` | *(empty)* | Comma-separated host allowlist; empty means any public host. |
| `TT_ALLOW_PRIVATE_HOSTS` | `false` | Allow loopback/private targets (local test media only). |

A `.env` entry overrides the built-in default; an explicit environment variable overrides `.env`.

---

## REST API

Interactive docs are served at **`/docs`** (OpenAPI at `/openapi.json`). When `TT_API_KEY` is set, send `X-API-Key: <value>` on every request; the web UI prompts for it once and stores it locally.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Liveness probe. |
| `GET` | `/api/system` | Versions, tool availability, worker settings, job counts. |
| `POST` | `/api/probe` | Analyse a video or playlist; returns entries and available qualities. |
| `POST` | `/api/jobs` | Create a download job (`url`, `options`, optional `entries`, `title`). |
| `GET` | `/api/jobs` | List jobs, newest first. |
| `GET` | `/api/jobs/{id}` | Full job with per-item state. |
| `POST` | `/api/jobs/{id}/cancel` | Cancel a job and all pending items. |
| `POST` | `/api/jobs/{id}/retry` | Re-queue failed/cancelled items. |
| `DELETE` | `/api/jobs/{id}` | Remove a job from history. |
| `POST` | `/api/jobs/{id}/items/{item_id}/cancel` | Cancel one item. |
| `POST` | `/api/jobs/{id}/items/{item_id}/retry` | Retry one item. |
| `POST` | `/api/jobs/{id}/open` | Open the job's file or folder in the OS file manager. |
| `GET` | `/api/events` | SSE stream: `snapshot`, `job`, and `item` events plus keep-alive pings. |
| `GET` | `/api/cookies` | List uploaded cookie files. |
| `POST` | `/api/cookies` | Upload a Netscape cookie file (multipart, max 3 MB). |

### Example: create a job

```bash
curl -X POST http://127.0.0.1:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.youtube.com/playlist?list=PL...",
    "options": {
      "mode": "playlist",
      "quality": "1080",
      "container": "mp4",
      "playlist_items": "1-5,8",
      "embed_thumbnail": true
    }
  }'
```

Quality values are `best`, `2160`, `1440`, `1080`, `720`, `480`, `360`, `worst`. Containers are `mp4`, `mkv`, `webm`, `original`. Audio formats are `mp3`, `m4a`, `opus`, `aac`, `flac`, `wav`, `original`.

### Example: follow progress

```bash
curl -N http://127.0.0.1:8000/api/events
```

---

## Docker

```bash
docker compose up --build -d
```

The compose file binds to `127.0.0.1:8000` and mounts `./downloads` and `./data`. The image installs ffmpeg, aria2c, and Node.js, runs as a non-root user, and ships a health check.

To publish it more widely, set an API key and change the port binding deliberately:

```bash
TT_API_KEY="$(openssl rand -hex 24)" docker compose up -d
```

```yaml
ports:
  - "8000:8000"
```

Put a TLS reverse proxy (Caddy, nginx, Traefik) in front of it if the traffic leaves your machine. TurboTube speaks plain HTTP and expects the proxy to terminate TLS.

---

## Security

TurboTube runs downloads on your behalf, so it treats URLs as untrusted input.

- **SSRF protection** — only `http`/`https`; URLs are resolved and rejected if they point at loopback, private, link-local, CGNAT, multicast, or reserved ranges. DNS answers are cached briefly and re-checked.
- **Optional allowlist** — set `TT_ALLOWED_HOSTS=youtube.com,youtu.be` to restrict targets outright.
- **API key** — set `TT_API_KEY` whenever the server is reachable by anyone else. Comparison is constant-time, and static assets stay public so the UI can load and prompt.
- **Path containment** — job output and "open folder" targets must stay inside `TT_DOWNLOAD_DIR`; output templates reject `/`, `\`, and `..`.
- **No shell execution** — external tools are invoked with argument lists, never through a shell.
- **Headers** — CSP, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, and frame denial are applied to responses.
- **Upload limits** — cookie uploads are capped at 3 MB and validated as Netscape format.
- **Proxy validation** — proxy URLs must be `http`/`https` and cannot target private networks.

Default deployment posture: bind to `127.0.0.1`, no API key, no CORS. That is safe for a single-user desktop. Any change to that posture should come with an API key and TLS.

Cookie files contain live session credentials. They live under `data/cookies/` and are excluded from Git and Docker build contexts.

---

## Project layout

```
app/
  __main__.py      CLI entry point
  cli.py           serve / probe / download / doctor
  config.py        Pydantic settings (TT_*)
  models.py        request/response and option models
  security.py      URL, proxy, cookie, and path validation
  youtube.py       yt-dlp integration, format selection, playlists
  manager.py       job queue, worker pool, retries, cancellation
  store.py         SQLite persistence (WAL)
  events.py        in-process pub/sub for SSE
  api.py           REST + SSE routes
  main.py          app factory, lifespan, auth, static mount
  static/          index.html, styles.css, app.js, favicon.svg
tests/             utils, security, formats, manager, API, SSE, CLI
```

Runtime state:

- `downloads/` — finished media.
- `data/turbotube.db` — jobs and items.
- `data/cookies/` — uploaded cookie files.
- `data/logs/` — reserved for job logs.

---

## Development

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest              # 51 tests, no network required
python -m pytest -k manager
python -m compileall app
python -m app serve --reload
```

Tests use a temporary download/data directory, a fake yt-dlp layer, and a stubbed DNS resolver, so they run offline and never touch real media.

---

## Troubleshooting

**`ffmpeg` not found / merging fails.**
Install ffmpeg and make sure `ffmpeg` and `ffprobe` are on `PATH`. Run `python -m app doctor`; it prints the resolved paths.

**HTTP 403 from `googlevideo.com`.**
YouTube is refusing a media URL, usually because no JavaScript runtime solved its challenge. Run `python -m app doctor` and check the `js runtime` and `ejs scripts` rows; install Node.js (or Deno) and `pip install yt-dlp-ejs`. If it persists, refresh yt-dlp (`pip install -U yt-dlp`), retry, or add browser cookies.

**Downloads are slower than expected.**
Check that aria2c is detected (`doctor`), raise workers before fragments for playlists, and confirm the output disk is not the bottleneck. Some ISPs throttle per-connection throughput, so more connections help; others throttle per-host, so fewer help.

**"Sign in to confirm you're not a bot" / age-restricted videos.**
Export cookies with a browser extension and use `--cookies cookies.txt`, or `--cookies-from-browser chrome`. Uploaded cookies are available in the UI under Advanced.

**A playlist item is unavailable.**
Private, deleted, and region-locked entries fail individually. The rest of the job continues and the job ends as `partial`; use retry on the failed items after fixing access.

**Job stuck after a crash.**
Jobs left in `running` are marked `interrupted` at startup. Use **Retry** in the UI, or set `TT_AUTO_RESUME=true` to re-queue them automatically.

**UI loads but the queue stays empty.**
An API key is probably required. The UI prompts on the first API call; clear the stored key in your browser's local storage if it is stale.

**Port already in use.**
`python -m app serve --port 8010`.

---

## Legal

TurboTube is a personal-use tool. Download only content you own or have permission to save, and respect YouTube's Terms of Service and local copyright law. The authors do not host, mirror, or distribute any media.

## License

MIT — see [LICENSE](LICENSE).
