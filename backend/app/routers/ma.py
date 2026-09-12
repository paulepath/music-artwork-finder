from __future__ import annotations

from fastapi import APIRouter

from ..config import get_settings
from ..models import JobKind
from ..worker import enqueue

router = APIRouter(prefix="/api/ma", tags=["music-assistant"])


@router.get("/status")
def ma_status():
    s = get_settings()
    return {
        "ws_url": s.ma_ws_url,
        "token_configured": s.ma_token() is not None,
        "token_file": str(s.ma_token_file),
    }


@router.post("/sync")
def ma_sync():
    return {"job_id": enqueue(JobKind.ma_sync)}
