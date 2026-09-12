"""Track/single artwork lookup across the trusted metadata services."""
from __future__ import annotations

import re

import httpx

from ..http import get_json
from ..matching import ReleaseMeta, TrackMeta
from .base import CandidateData

MB = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org"
ITUNES = "https://itunes.apple.com/search"
DEEZER = "https://api.deezer.com/search/track"
_SIZE_RE = re.compile(r"/\d+x\d+bb\.(jpg|png)")


def _artist_credit(item: dict) -> str:
    return "".join(
        part.get("name", "") + part.get("joinphrase", "")
        for part in (item.get("artist-credit") or [])
    )


async def _musicbrainz(client: httpx.AsyncClient, track: TrackMeta) -> list[CandidateData]:
    recordings: list[tuple[dict, bool]] = []
    if track.musicbrainz_trackid:
        item = await get_json(
            client, f"{MB}/recording/{track.musicbrainz_trackid}",
            params={"fmt": "json", "inc": "artist-credits+releases"},
        )
        if isinstance(item, dict):
            recordings.append((item, True))
    if not recordings:
        query = f'recording:"{track.title}"'
        if track.artist:
            query += f' AND artist:"{track.artist}"'
        data = await get_json(client, f"{MB}/recording", params={"fmt": "json", "query": query, "limit": 8})
        recordings.extend((item, False) for item in (data or {}).get("recordings", [])[:8]
                          if int(item.get("score", 0)) >= 70)

    out: list[CandidateData] = []
    seen: set[str] = set()
    for recording, exact in recordings:
        for release in (recording.get("releases") or [])[:8]:
            rid = release.get("id")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            info = await get_json(client, f"{CAA}/release/{rid}")
            if not info or not any(image.get("front") for image in info.get("images", [])):
                continue
            out.append(CandidateData(
                source="musicbrainz",
                image_url=f"{CAA}/release/{rid}/front-500",
                provenance_url=f"https://musicbrainz.org/release/{rid}",
                release=ReleaseMeta(
                    source="musicbrainz", title=recording.get("title", ""),
                    artist=_artist_credit(recording), mbid=recording.get("id"),
                ),
                extra={"evidence_kind": "exact_id" if exact else "search"},
            ))
    return out


async def _itunes(client: httpx.AsyncClient, track: TrackMeta) -> list[CandidateData]:
    term = f"{track.artist} {track.title}".strip()
    data = await get_json(client, ITUNES, params={"term": term, "entity": "song", "limit": 8, "media": "music"})
    out: list[CandidateData] = []
    for item in (data or {}).get("results", [])[:8]:
        art = item.get("artworkUrl100") or item.get("artworkUrl60")
        if not art:
            continue
        art = _SIZE_RE.sub("/1200x1200bb.jpg", art)
        out.append(CandidateData(
            source="itunes", image_url=art,
            provenance_url=item.get("trackViewUrl") or item.get("collectionViewUrl", ""),
            width=1200, height=1200,
            release=ReleaseMeta(source="itunes", title=item.get("trackName", ""),
                                artist=item.get("artistName", "")),
            extra={"collection": item.get("collectionName", ""), "evidence_kind": "search"},
        ))
    return out


async def _deezer(client: httpx.AsyncClient, track: TrackMeta) -> list[CandidateData]:
    query = f'{track.artist} {track.title}'.strip()
    data = await get_json(client, DEEZER, params={"q": query, "limit": 8})
    out: list[CandidateData] = []
    for item in (data or {}).get("data", [])[:8]:
        album = item.get("album") or {}
        art = album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium")
        if not art:
            continue
        out.append(CandidateData(
            source="deezer", image_url=art, provenance_url=item.get("link", ""),
            width=1000 if album.get("cover_xl") else 500,
            height=1000 if album.get("cover_xl") else 500,
            release=ReleaseMeta(source="deezer", title=item.get("title", ""),
                                artist=(item.get("artist") or {}).get("name", "")),
            extra={"collection": album.get("title", ""), "evidence_kind": "search"},
        ))
    return out


async def find_track_candidates(client: httpx.AsyncClient, track: TrackMeta) -> list[CandidateData]:
    """Return all trusted observations; a caller isolates individual provider failures."""
    out: list[CandidateData] = []
    for finder in (_musicbrainz, _itunes, _deezer):
        try:
            out.extend(await finder(client, track))
        except (httpx.HTTPError, ValueError):
            continue
    return out

