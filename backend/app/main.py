from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import init_db
from .routers import albums, audit, candidates, ma, scan, search
from .worker import worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("artwork")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    s = get_settings()
    s.ensure_dirs()
    init_db()
    log.info("music_root=%s data_dir=%s", s.music_root, s.data_dir)
    worker.start()
    try:
        yield
    finally:
        await worker.stop()


app = FastAPI(title="artwork-recovery", version="0.1.0", lifespan=lifespan)

for r in (scan.router, albums.router, audit.router, candidates.router, ma.router, search.router):
    app.include_router(r)


@app.get("/api/health")
def health():
    return {"ok": True}


_settings = get_settings()
if _settings.static_dir.is_dir():
    app.mount("/assets", StaticFiles(directory=_settings.static_dir / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):  # noqa: ARG001 - SPA history fallback
        index = _settings.static_dir / "index.html"
        return FileResponse(index)
