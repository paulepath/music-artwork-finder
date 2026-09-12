"""Shared async HTTP client with polite per-domain rate limiting."""
from __future__ import annotations

import time
import asyncio
from urllib.parse import urlsplit

import httpx

from .config import get_settings

_settings = get_settings()
_last_hit: dict[str, float] = {}
_locks: dict[str, asyncio.Lock] = {}


async def _throttle(url: str) -> None:
    host = urlsplit(url).hostname or "_default"
    lock = _locks.setdefault(host, asyncio.Lock())
    async with lock:
        gap = _settings.rate_limits.get(host, _settings.rate_limits.get("_default", 0.25))
        now = time.monotonic()
        wait = gap - (now - _last_hit.get(host, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        _last_hit[host] = time.monotonic()


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": _settings.http_user_agent, "Accept": "application/json"},
        timeout=httpx.Timeout(20.0),
        follow_redirects=True,
    )


async def get_json(client: httpx.AsyncClient, url: str, **kw) -> dict | list | None:
    host = urlsplit(url).hostname or "_default"
    for attempt in range(3):
        await _throttle(url)
        try:
            r = await client.get(url, **kw)
            if r.status_code == 404:
                return None
            if r.status_code == 503 and attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        finally:
            _last_hit[host] = time.monotonic()


async def get_bytes(client: httpx.AsyncClient, url: str, **kw) -> tuple[bytes, str]:
    await _throttle(url)
    r = await client.get(url, **kw)
    r.raise_for_status()
    return r.content, r.headers.get("content-type", "")
