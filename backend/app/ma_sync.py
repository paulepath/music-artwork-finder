"""Ask Music Assistant to re-scan the filesystem so new artwork is picked up.

Best-effort and optional. Requires a long-lived token supplied by the user in
``$ARTWORK_DATA_DIR/secrets/ma_token`` (or the ``MA_TOKEN`` env var). Never restarts
MA — a restart would interrupt playback.
"""
from __future__ import annotations

import json
import logging
import uuid

from .config import get_settings

log = logging.getLogger("artwork.ma_sync")


class MASyncResult:
    def __init__(self, ok: bool, detail: str):
        self.ok = ok
        self.detail = detail

    def as_dict(self) -> dict:
        return {"ok": self.ok, "detail": self.detail}


async def trigger_sync(media_types: list[str] | None = None) -> MASyncResult:
    s = get_settings()
    token = s.ma_token()
    if not token:
        return MASyncResult(False, "no MA token configured; request a filesystem scan in MA manually")

    try:
        import websockets
    except ImportError:  # pragma: no cover
        return MASyncResult(False, "websockets library not installed")

    try:
        async with websockets.connect(s.ma_ws_url, open_timeout=10, close_timeout=5) as ws:
            hello = json.loads(await ws.recv())          # server info frame
            log.info("MA server info: %s", hello.get("server_version"))

            await ws.send(json.dumps({
                "command": "auth", "message_id": str(uuid.uuid4()),
                "args": {"token": token},
            }))
            auth_reply = json.loads(await ws.recv())
            if auth_reply.get("error") or auth_reply.get("error_code"):
                return MASyncResult(False, f"MA auth failed: {auth_reply.get('error') or auth_reply}")

            await ws.send(json.dumps({
                "command": "music/sync", "message_id": str(uuid.uuid4()),
                "args": {"media_types": media_types} if media_types else {},
            }))
            sync_reply = json.loads(await ws.recv())
            if sync_reply.get("error") or sync_reply.get("error_code"):
                return MASyncResult(False, f"music/sync rejected: {sync_reply.get('error') or sync_reply}")
            return MASyncResult(True, "music/sync accepted by Music Assistant")
    except Exception as e:  # noqa: BLE001
        return MASyncResult(False, f"MA websocket error: {e}")
