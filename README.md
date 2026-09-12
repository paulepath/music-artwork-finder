# Music Artwork Finder

Web app that finds missing album/track artwork for a local music library and embeds it
into audio file tags (front-cover `APIC` / `METADATA_BLOCK_PICTURE`) so **Music Assistant** and other media players display it.

Trusted metadata sources are tried first (MusicBrainz + Cover Art Archive, iTunes,
Deezer). A headless Google Images scrape is a **manually-approved last resort** — its
results are clustered by perceptual hash so a human can pick the consensus cover in the
web UI. Nothing fuzzy is ever written without explicit per-album approval, and every
write is backed up and undoable.

## Layout

```
backend/    FastAPI + SQLite + Mutagen + Playwright
frontend/   React + Vite SPA (built into backend/app/static at image build time)
Dockerfile  multi-stage: node build -> playwright-python runtime
docker-compose.yml  ready-to-run compose definition
```

## Running with Docker

```bash
docker compose up -d
```

## Local dev

```bash
# backend
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium        # only needed for the Google source
export MUSIC_ROOT=/path/to/sample/music
export ARTWORK_DATA_DIR=./data
uvicorn app.main:app --reload --port 8000

# frontend (separate shell, proxies /api to :8000)
cd frontend
npm install
npm run dev
```

## Tests

```bash
cd backend && pytest
```

## Config (env vars)

| var | default | meaning |
|-----|---------|---------|
| `MUSIC_ROOT` | `/music` | root of the music library (mounted rw) |
| `ARTWORK_DATA_DIR` | `/config` | sqlite db, backups, image cache, secrets |
| `MA_WS_URL` | `ws://192.168.0.120:8095/ws` | Music Assistant websocket |
| `MA_TOKEN_FILE` | `$ARTWORK_DATA_DIR/secrets/ma_token` | long-lived MA token (optional) |
| `PLEX_URL` | unset | Plex server URL, e.g. `http://192.168.1.50:32400` (optional, read-only) |
| `PLEX_TOKEN_FILE` | `$ARTWORK_DATA_DIR/secrets/plex_token` | Plex token file; alternatively set `PLEX_TOKEN` as a secret env var |
| `PLEX_PATH_MAPS` | unset | comma-separated local-to-Plex path maps, e.g. `/music=/volume4/music` |
| `AUDIO_EXTENSIONS` | `.mp3,.m4a,.flac,.ogg,.opus` | scanned file types |
| `HTTP_USER_AGENT` | `artwork-recovery/0.1 (+homelab)` | MusicBrainz requires a real UA |

When Plex is configured, its album image is offered only when Plex maps **every
track in the local album directory** to the same Plex album. It remains a
review-only candidate; Plex is never used as Music Assistant's provider and its
cache is never scraped.
