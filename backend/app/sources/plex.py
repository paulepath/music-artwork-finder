"""Read-only Plex artwork discovery, guarded by exact audio-file path matching."""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from ..config import get_settings
from ..matching import GroupMeta, ReleaseMeta
from .base import CandidateData


@dataclass(frozen=True)
class _PlexAlbum:
    rating_key: str
    title: str
    artist: str
    thumb: str


class PlexSource:
    """Index Plex tracks once, then only offer a common album for every local path.

    This deliberately does not search by album title and does not inspect Plex's
    thumbnail cache.  Its only authority is Plex metadata tied to the exact music
    files that are about to be written.
    """
    name = "plex"
    _TTL_SECONDS = 15 * 60

    def __init__(self) -> None:
        self._index: dict[str, set[_PlexAlbum]] = {}
        self._loaded_at = 0.0
        self._unavailable_until = 0.0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        s = get_settings()
        return bool(s.plex_url and s.plex_token())

    def _headers(self) -> dict[str, str]:
        token = get_settings().plex_token()
        return {"X-Plex-Token": token} if token else {}

    def _local_to_plex_paths(self, path: str) -> set[str]:
        s = get_settings()
        path = path.rstrip("/")
        # Scanner paths are deliberately relative to MUSIC_ROOT (e.g.
        # /Artist/Album/01.mp3).  Also consider their in-container absolute
        # form so PLEX_PATH_MAPS can naturally say /music=/volume4/music.
        rooted = s.music_root.as_posix().rstrip("/") + path
        out = {path, rooted}
        for local, plex in s.plex_path_maps:
            for candidate in (path, rooted):
                if candidate == local or candidate.startswith(local + "/"):
                    out.add(plex + candidate[len(local):])
        return out

    async def _json(self, client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict:
        s = get_settings()
        response = await client.get(s.plex_url + path, params=params, headers=self._headers())
        response.raise_for_status()
        return response.json()

    async def _refresh_index(self, client: httpx.AsyncClient) -> None:
        if not self.enabled:
            self._index = {}
            return
        sections = await self._json(client, "/library/sections")
        directories = sections.get("MediaContainer", {}).get("Directory", [])
        music_keys = [str(d["key"]) for d in directories if d.get("type") == "artist" and d.get("key")]
        index: dict[str, set[_PlexAlbum]] = defaultdict(set)
        for key in music_keys:
            start = 0
            while True:
                data = await self._json(client, f"/library/sections/{quote(key, safe='')}/all", {
                    "type": 10, "X-Plex-Container-Start": start,
                    "X-Plex-Container-Size": 500, "includeMedia": 1,
                })
                container = data.get("MediaContainer", {})
                tracks = [x for x in container.get("Metadata", []) if x.get("type") == "track"]
                for track in tracks:
                    rating_key = str(track.get("parentRatingKey") or "")
                    thumb = track.get("parentThumb") or ""
                    if not rating_key or not thumb:
                        continue
                    album = _PlexAlbum(rating_key, str(track.get("parentTitle") or ""),
                                       str(track.get("grandparentTitle") or ""), str(thumb))
                    for media in track.get("Media", []):
                        for part in media.get("Part", []):
                            filename = part.get("file")
                            if filename:
                                index[str(filename).rstrip("/")].add(album)
                size = int(container.get("size", len(tracks)) or 0)
                total = int(container.get("totalSize", 0) or 0)
                start += max(size, len(tracks))
                if not tracks or (total and start >= total) or (not total and len(tracks) < 500):
                    break
        self._index = dict(index)
        self._loaded_at = time.monotonic()

    async def _ensure_index(self, client: httpx.AsyncClient) -> None:
        if time.monotonic() < self._unavailable_until:
            return
        if self._loaded_at and time.monotonic() - self._loaded_at < self._TTL_SECONDS:
            return
        async with self._lock:
            if not self._loaded_at or time.monotonic() - self._loaded_at >= self._TTL_SECONDS:
                try:
                    await self._refresh_index(client)
                except httpx.HTTPError:
                    # A NAS routing outage must not add a timeout to every
                    # album.  The next candidate run retries after the cooldown.
                    self._unavailable_until = time.monotonic() + self._TTL_SECONDS
                    raise

    async def find(self, client: httpx.AsyncClient, group: GroupMeta) -> list[CandidateData]:
        if not self.enabled or not group.track_paths:
            return []
        await self._ensure_index(client)
        if time.monotonic() < self._unavailable_until:
            return []
        matching: set[_PlexAlbum] | None = None
        for local_path in group.track_paths:
            albums: set[_PlexAlbum] = set()
            for plex_path in self._local_to_plex_paths(local_path):
                albums.update(self._index.get(plex_path, set()))
            matching = albums if matching is None else matching & albums
        if not matching:
            return []
        s = get_settings()
        result: list[CandidateData] = []
        for album in matching:
            thumb_url = album.thumb if album.thumb.startswith("http") else s.plex_url + album.thumb
            result.append(CandidateData(
                source=self.name, image_url=thumb_url,
                provenance_url=f"{s.plex_url}/web/index.html#!/server/{album.rating_key}",
                release=ReleaseMeta(source=self.name, title=album.title, artist=album.artist),
                extra={"force_review": True, "path_match": "all local tracks map to this Plex album"},
            ))
        return result
