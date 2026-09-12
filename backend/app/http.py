"""Shared async HTTP client with polite per-domain rate limiting."""
from __future__ import annotations

import time
from urllib.parse import urlsplit

import httpx

from .config import get_settings

_settings = get_settings()
_last_hit: dict[str, float] = {}


async def _throttle(url: str) -> None:
    host = urlsplit(url).hostname or "_default"
    gap = _settings.rate_limits.get(host, _settings.rate_limits.get("_default", 0.25))
    now = time.monotonic()
    wait = gap - (now - _last_hit.get(host, 0.0))
    if wait > 0:
        import asyncio
        await asyncio.sleep(wait)
    _last_hit[host] = time.monotonic()


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": _settings.http_user_agent, "Accept": "application/json"},
        timeout=httpx.Timeout(20.0),
        follow_redirects=True,
    )


async def get_json(client: httpx.AsyncClient, url: str, **kw) -> dict | list | None:
    await _throttle(url)
    r = await client.get(url, **kw)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


async def get_bytes(client: httpx.AsyncClient, url: str, **kw) -> tuple[bytes, str]:
    await _throttle(url)
    r = await client.get(url, **kw)
    r.raise_for_status()
    return r.content, r.headers.get("content-type", "")
