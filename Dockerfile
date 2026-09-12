# syntax=docker/dockerfile:1

# ---- stage 1: build the React SPA -------------------------------------
FROM node:20-bookworm-slim AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
# The repository currently has no lockfile.  Prefer deterministic npm ci as
# soon as one is committed, while retaining a buildable first checkout.
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
COPY frontend/ ./
# tsc typecheck + build; override vite outDir (config points at ../backend for local dev)
RUN npx tsc -b && npx vite build --outDir dist --emptyOutDir

# ---- stage 2: python runtime with Chromium (Playwright) --------------
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy AS runtime
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ARTWORK_DATA_DIR=/config \
    MUSIC_ROOT=/music
WORKDIR /app

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY --from=frontend /fe/dist ./app/static
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000
VOLUME ["/config"]
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
