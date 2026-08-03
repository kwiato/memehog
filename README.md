# 🐗 Memehog

**Self-hosted meme library for your Raspberry Pi.** Send Instagram/TikTok links (or media files) to a Telegram bot, and Memehog downloads everything into one library with a searchable web gallery.

- 🤖 **Telegram bot** — send a link or a photo/video/GIF, it lands in your library
- ⬇️ **Downloads** Instagram posts/reels/carousels, TikToks and direct image/video links (yt-dlp + gallery-dl)
- 🔍 **Full-text search** over captions, tags and filenames (SQLite FTS5) — and, with a VLM configured, over the text on the memes and AI-written descriptions of what they show
- 🖼️ **Web gallery** — masonry grid, infinite scroll, lightbox, tagging, upload by file, URL or drag&drop onto the page
- 🔌 **REST API** with token auth — the future Chrome extension and Android app will use it
- 👥 **Multi-user** — friends send `/register` to the bot, you approve them with one tap in Telegram (or in web Settings)
- 🗳 **Guest submissions** — strangers can send the bot a meme *file* (never links); it's quarantined until you vote 👍/👎 in Telegram, and an accepted sender gets a random meme back as a thank-you
- 🔥 **Spicy mode** — memes tagged `spicy` are hidden from the default gallery and only appear behind the 🔥 button; their files live in a separate `library/spicy/` folder, and uploads made while 🔥 mode is on land there directly
- 🌙 **Nightly maintenance** — converts `webp → jpg` and `webm → mp4` (formats many apps choke on); schedule configurable in Settings
- 🦾 Designed for small ARM boards: one container, one process, SQLite, no external services

## Quick start

Requirements: Docker with the compose plugin (`curl -fsSL https://get.docker.com | sh`).

```bash
git clone https://github.com/YOUR_USER/memehog.git
cd memehog
./install.sh
```

The wizard asks for:

1. **Telegram bot token** — talk to [@BotFather](https://t.me/BotFather), send `/newbot`, paste the token. (Optional — skip it to run the web UI only.)
2. **Allowed Telegram IDs** — message [@userinfobot](https://t.me/userinfobot) to get your numeric ID. Only these users can talk to the bot.
3. **Data directory** — where memes, thumbnails and the database live.
4. **Web UI port** — default `2137`.

It generates an `API_TOKEN`, writes `.env` and starts the stack. Open `http://<pi-address>:2137` and send your bot a meme.

### Updating

```bash
git pull && docker compose up -d --build
```

> **Upgrading to v0.3+:** the container now runs as a non-root user (uid 1000)
> for security. Make your data directory writable for it once:
> `sudo chown -R 1000:1000 <your data dir>`. Bonus: files in the library now
> belong to the default Pi user instead of root, so you can manage them over
> SMB/SFTP/remote desktop without sudo.

### Deploy with Portainer

No shell needed — deploy straight from this repository:

1. **Stacks → Add stack → Repository**
2. Repository URL: `https://github.com/kwiato/memehog`, reference: `refs/heads/main`, compose path: `docker-compose.yml`
3. Add **environment variables** (this replaces the `install.sh` wizard):

   | Variable | Value |
   |---|---|
   | `API_TOKEN` | required — generate one: `openssl rand -hex 24` |
   | `HOST_DATA_DIR` | **absolute** path on the host, e.g. `/srv/memehog` (don't leave the relative default — a git stack's working dir is ephemeral) |
   | `BOT_TOKEN` | token from @BotFather (optional) |
   | `ALLOWED_TELEGRAM_IDS` | your Telegram ID(s), comma-separated |
   | `PORT` | optional, default `2137` |

4. Deploy. Updates: enable *GitOps updates* (polling) in the stack, or hit *Pull and redeploy* after a push.

Prefer the **Web editor** instead? CI publishes a multi-arch image (ARM64 +
x86_64) to Docker Hub (`hexdesign/memehog`) and GHCR (`ghcr.io/kwiato/memehog`),
so you can paste this and set the same environment variables as above:

```yaml
services:
  memehog:
    image: hexdesign/memehog:latest
    container_name: memehog
    restart: unless-stopped
    environment:
      BOT_TOKEN: ${BOT_TOKEN:-}
      ALLOWED_TELEGRAM_IDS: ${ALLOWED_TELEGRAM_IDS:-}
      API_TOKEN: ${API_TOKEN:?set API_TOKEN}
      PORT: ${PORT:-2137}
      DATA_DIR: /data
      COOKIES_FILE: ${COOKIES_FILE:-}
    ports:
      - "${PORT:-2137}:${PORT:-2137}"
    volumes:
      - ${HOST_DATA_DIR:?set HOST_DATA_DIR, e.g. /srv/memehog}:/data
```

Updating: *Recreate* the container with *Re-pull image* enabled (or use Watchtower).

## Usage

**Telegram bot** — send it any of:

- an Instagram post / reel / carousel link
- a TikTok link
- a direct link to an image or video file
- a photo, video or GIF straight from your phone (the caption becomes searchable text; write `nsfw` anywhere in it and the meme lands straight in the 🔥 spicy stash — works for links and guest submissions too)

**Web UI** — browse, search (`kot w kapeluszu` matches prefixes, so partial words work), filter by type/tag, click a meme for the [PhotoSwipe](https://photoswipe.com/) lightbox — pinch/scroll to zoom, swipe or use arrow keys to browse — with a bar underneath to tag, download, share or delete it (delete hides under the ⋮ menu, file details under the ⌄ arrow). The floating **＋ button** (bottom right) opens the upload dialog for files or a link to download — or skip it and **drag&drop files anywhere on the page**. The **🔥 button** switches to spicy-only view; while it's on, uploads and drops are saved as spicy right away. Mark or unmark an existing meme from its ⋮ menu. The **☰ menu** opens the Settings page (Telegram clients, AI indexing and model management, nightly maintenance hour) and About.

**Library layout** — files live under `library/YYYY/<hash>.<ext>` on disk (spicy ones under `library/spicy/YYYY/…` — toggling 🔥 on a meme moves the file), deduplicated by content hash. The nightly job (default 03:00, configurable in Settings) transcodes `webp`/`webm` into universally supported `jpg`/`mp4`.

**Access for friends** — `ALLOWED_TELEGRAM_IDS` holds the owner account(s). Anyone else who messages the bot gets a hint to send `/register`; the owner receives the request in Telegram with ✅/❌ buttons, and approved users land in Settings → *Additional Telegram clients*, where they can also be added or removed manually.

**Guest submissions** — someone who isn't registered can still send the bot a meme as a *file* (photo/video/GIF). Links from guests are never downloaded. The file is quarantined in `pending/` (outside the library, no processing), deduplicated against the library, and rate-limited (3 awaiting votes, 10 per day per sender). The owner gets the meme in Telegram with 👍/👎 buttons: 👍 ingests it into the library and sends the submitter a random (non-spicy) meme as a reward, 👎 deletes it.

## Configuration

Everything lives in `.env` (see [.env.example](.env.example)):

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | *(empty)* | Telegram bot token; empty disables the bot |
| `ALLOWED_TELEGRAM_IDS` | *(empty)* | Comma-separated user IDs allowed to use the bot |
| `API_TOKEN` | — | Bearer token for `/api/v1/*` (generated by `install.sh`) |
| `PORT` | `2137` | Web UI / API port |
| `HOST_DATA_DIR` | `./data` | Host directory mounted as the data volume (Docker) |
| `DATA_DIR` | `data` | Data directory (bare-metal runs; `/data` inside Docker) |
| `COOKIES_FILE` | *(empty)* | Netscape-format cookies file for Instagram/TikTok¹ |
| `SCAN_CRON` | `0 3 * * *` | Schedule for the nightly maintenance (transcode + VLM index) |
| `VLM_BASE_URL` | *(empty)* | OpenAI-compatible vision endpoint for the nightly indexer² |
| `VLM_API_KEY` | *(empty)* | API key for the VLM endpoint |
| `VLM_MODEL` | *(empty)* | Vision model name, e.g. `gemini-3.5-flash` |
| `VLM_LANGUAGE` | `English` | Language the meme descriptions are written in |
| `VLM_RPM` | `10` | Indexer request rate — keep under your provider's free-tier limit |
| `VLM_MAX_PER_RUN` | `200` | Max items indexed per night (spreads backfills over several nights) |
| `VLM_INDEX_SPICY` | `false` | Also send spicy memes to the VLM (see privacy note²) |
| `VLM_AUTO_TAG` | `true` | Let the indexer attach tags (prefers your existing tags; AI tags show dashed) |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

¹ Instagram increasingly requires login for downloads. Export cookies from your browser with an extension like *Get cookies.txt LOCALLY*, drop the file into the data directory and set `COOKIES_FILE=/data/cookies.txt`.

² The nightly indexer sends each new meme's thumbnail to a vision model and stores the OCR'd text plus a short description in the search index. Easiest setup: **Settings (☰ → ⚙️) → AI models → ＋ Add model** — pick a provider preset (the info box links to where you get an API key), paste the key, hit *Test connection*, save. **Several models can be active at once**: each indexes new memes independently and keeps its own searchable text, so one provider having a bad night doesn't leave memes unindexed — and the search bar gets a dropdown to query one model's data or all of them. Saved models can also be compared head-to-head with the built-in benchmark. Web settings override `.env`. Any OpenAI-compatible endpoint works — the free Gemini tier (key at [aistudio.google.com](https://aistudio.google.com), `VLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai`) comfortably covers a personal library; OpenRouter, Groq, Mistral or a local Ollama work too. **Privacy note:** free API tiers may use your inputs for model training, which is why spicy memes are excluded unless `VLM_INDEX_SPICY=true`.

### Remote access

Memehog has no login page — it's designed to sit on a private network. The recommended setup is [Tailscale](https://tailscale.com/): install it on the Pi and your devices, then browse `http://<pi-tailnet-name>:2137` from anywhere. Only the `/api/v1/*` endpoints are additionally protected with the Bearer token, so automations (and the future browser extension) can authenticate.

**Do not port-forward Memehog directly to the internet.**

## API

All endpoints (except `/api/v1/health`) require `Authorization: Bearer <API_TOKEN>`.

```bash
# queue a URL for download
curl -X POST http://pi:2137/api/v1/jobs \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@user/video/123"}'

# search
curl "http://pi:2137/api/v1/items?q=cat" -H "Authorization: Bearer $TOKEN"

# upload a file
curl -X POST http://pi:2137/api/v1/items \
  -H "Authorization: Bearer $TOKEN" -F "file=@meme.jpg"
```

Interactive docs: `http://pi:2137/api/docs`.

## Architecture

One Python process, one container, one SQLite database:

```
Telegram bot (aiogram) ─┐
Web UI (FastAPI+HTMX) ──┼─→ download queue ─→ yt-dlp / gallery-dl / direct fetch
REST API ───────────────┘         │
                                  ▼
                    library/YYYY/MM/<sha>.<ext>  +  thumbnails (Pillow/ffmpeg)
                                  │
                                  ▼
                SQLite: items · tags · jobs · FTS5 index · embeddings (future)
```

- Files are deduplicated by SHA-256 — sending the same meme twice stores it once.
- The download queue is durable (survives restarts) and processes jobs sequentially, which keeps the Pi responsive and rate-limiters happy.
- Search is a pluggable `SearchBackend`; the FTS5 index already has an `ocr_text` column and the DB an `embeddings` table, so OCR and vector search can be added without a schema migration.

## Roadmap

- [x] **Nightly VLM indexing** — OCR + AI descriptions of new items via any OpenAI-compatible vision API, feeding the `ocr_text` FTS column
- [ ] **Vector search** — text embeddings over the VLM descriptions for fuzzy semantic queries (the `embeddings` table is ready)
- [ ] **Chrome extension** — right-click → *Save to Memehog*
- [ ] **Android app** — share sheet target + gallery

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
cp .env.example .env   # tweak as needed
python -m memehog      # http://localhost:2137
pytest
```

ffmpeg is optional in development (video thumbnails are skipped without it) but required in production — the Docker image includes it.

**Styles** are written in SCSS (`src/memehog/web/scss/`, theme variables in
`_variables.scss`); `static/style.css` is generated, not checked in. The Docker
build compiles it with the standalone dart-sass binary (multi-arch, no Node);
in development it compiles automatically on first start via libsass (part of
the `dev` extras), or manually with `python scripts/build_css.py` after
editing.

## License

[MIT](LICENSE)
