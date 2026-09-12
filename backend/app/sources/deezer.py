"""Deezer public API (keyless). Good coverage for compilations and dance/chill labels."""
from __future__ import annotations

import httpx

from ..http import get_json
from ..matching import GroupMeta, ReleaseMeta
from .base import CandidateData

SEARCH = "https://api.deezer.com/search/album"


class DeezerSource:
    name = "deezer"

    async def find(self, client: httpx.AsyncClient, group: GroupMeta) -> list[CandidateData]:
        q = f'{group.album_artist} {group.album}'.strip()
        if not q or q == "?":
            return []
        data = await get_json(client, SEARCH, params={"q": q, "limit": 8})
        out: list[CandidateData] = []
        for item in (data or {}).get("data", [])[:8]:
            art = item.get("cover_xl") or item.get("cover_big") or item.get("cover_medium")
            if not art:
                continue
            out.append(CandidateData(
                source="deezer",
                image_url=art,
                provenance_url=item.get("link", ""),
                width=1000 if item.get("cover_xl") else 500,
                height=1000 if item.get("cover_xl") else 500,
                release=ReleaseMeta(
                    source="deezer",
                    title=item.get("title", ""),
                    artist=(item.get("artist") or {}).get("name", ""),
                    track_count=item.get("nb_tracks"),
                ),
            ))
        return out
