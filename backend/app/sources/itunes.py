"""iTunes Search API (keyless). Returns 100x100 art URLs that we upscale to 1200px."""
from __future__ import annotations

import re

import httpx

from ..http import get_json
from ..matching import GroupMeta, ReleaseMeta
from .base import CandidateData

SEARCH = "https://itunes.apple.com/search"
_SIZE_RE = re.compile(r"/\d+x\d+bb\.(jpg|png)")


def _upscale(url: str, px: int = 1200) -> str:
    return _SIZE_RE.sub(f"/{px}x{px}bb.jpg", url)


class ITunesSource:
    name = "itunes"

    async def find(self, client: httpx.AsyncClient, group: GroupMeta) -> list[CandidateData]:
        term = f"{group.album_artist} {group.album}".strip()
        if not term or term == "?":
            return []
        data = await get_json(
            client, SEARCH,
            params={"term": term, "entity": "album", "limit": 8, "media": "music"},
        )
        out: list[CandidateData] = []
        for item in (data or {}).get("results", [])[:8]:
            art = item.get("artworkUrl100") or item.get("artworkUrl60")
            if not art:
                continue
            year = None
            rd = item.get("releaseDate", "")
            if rd[:4].isdigit():
                year = int(rd[:4])
            out.append(CandidateData(
                source="itunes",
                image_url=_upscale(art),
                provenance_url=item.get("collectionViewUrl", ""),
                width=1200, height=1200,
                release=ReleaseMeta(
                    source="itunes",
                    title=item.get("collectionName", ""),
                    artist=item.get("artistName", ""),
                    year=year,
                    track_count=item.get("trackCount"),
                ),
            ))
        return out
