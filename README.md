# artwork-recovery

Track-first web app for finding and repairing artwork in a local music library. Select
one or more tracks, or browse to a folder and include it recursively, then watch a
durable background search session update in the browser.

Trusted metadata sources are tried first (MusicBrainz + Cover Art Archive, iTunes,
Deezer, and optional Plex). Google Images is used automatically when those sources do
not produce a high-confidence result. Exact and near-identical images are grouped by
perceptual hash; agreement from independent sources raises the result's score, while
repeated Google results have capped influence.

Every track shows its current embedded artwork and separate candidate groups for album
art and track/single artwork. The best choices are preselected, but **nothing is written
until it is explicitly approved and applied**. Writes support MP3, FLAC, Ogg/Opus and
M4A, preserve unrelated pictures, create recoverable backups, and are recorded in the
audit log. M4A files store album art first and track art second in `covr`. A successful
apply also queues a Music Assistant library refresh when its token is configured.

Full background / spec: `../CLAUDE_HANDOFF.md`.

## Layout

```
backend/    FastAPI + SQLite + Mutagen + Playwright
frontend/   React + Vite SPA (built into backend/app/static at image build time)
Dockerfile  multi-stage: node build -> playwright-python runtime
```

The production compose definition is in `deploy/artwork-recovery.compose.yml`
(Portainer, Synology NAS endpoint ID 2).

## Workflow

1. Run **Rescan library** from Maintenance after adding or retagging music.
2. Open **Find artwork**, search/filter tracks or choose a folder, and start a session.
3. Review grouped album and track/single candidates while the search progresses.
4. Approve individual choices, or approve all recommended high/medium-confidence
   choices, then apply them.

Folders larger than 100 tracks require confirmation. Search sessions can be paused,
resumed, cancelled, and retried. `cover.jpg` is updated only when a complete selected
folder agrees on one approved album image.

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
