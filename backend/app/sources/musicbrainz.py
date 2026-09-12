"""MusicBrainz release lookup + Cover Art Archive images.

Priority path:
  1. If the group already carries a release MBID -> fetch that release + CAA front.
  2. Else search releases by artist+album, keep close matches, attach CAA front.
  3. Release-group front is offered separately, flagged ``is_release_group_only``.
"""
from __future__ import annotations

import httpx

from ..http import get_json
from ..matching import GroupMeta, ReleaseMeta
from .base import CandidateData

MB = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org"


def _caa_front(mbid: str, kind: str = "release") -> str:
    return f"{CAA}/{kind}/{mbid}/front-500"


def _release_meta(rel: dict, *, rg_only: bool = False) -> ReleaseMeta:
    artist = ""
    ac = rel.get("artist-credit") or []
    if ac:
        artist = "".join(part.get("name", "") + part.get("joinphrase", "") for part in ac)
    year = None
    date = rel.get("date") or (rel.get("release-group") or {}).get("first-release-date") or ""
    if date[:4].isdigit():
        year = int(date[:4])
    tc = None
    media = rel.get("media") or []
    if media:
        tc = sum(m.get("track-count", 0) for m in media) or None
    return ReleaseMeta(
        source="musicbrainz",
        title=rel.get("title", ""),
        artist=artist,
        year=year,
        track_count=tc,
        mbid=None if rg_only else rel.get("id"),
        release_group_id=(rel.get("release-group") or {}).get("id") or rel.get("id"),
        barcode=rel.get("barcode"),
        is_release_group_only=rg_only,
    )


class MusicBrainzSource:
    name = "musicbrainz"

    async def find(self, client: httpx.AsyncClient, group: GroupMeta) -> list[CandidateData]:
        out: list[CandidateData] = []
        seen_release_ids: set[str] = set()

        async def add_release(rel: dict):
            rid = rel.get("id")
            if not rid or rid in seen_release_ids:
                return
            seen_release_ids.add(rid)
            # confirm CAA actually has a front image for this release
            info = await get_json(client, f"{CAA}/release/{rid}")
            if not info:
                return
            has_front = any(img.get("front") for img in info.get("images", []))
            if not has_front:
                return
            out.append(CandidateData(
                source="musicbrainz",
                image_url=_caa_front(rid),
                provenance_url=f"https://musicbrainz.org/release/{rid}",
                release=_release_meta(rel),
            ))

        if group.mbid:
            rel = await get_json(
                client, f"{MB}/release/{group.mbid}",
                params={"fmt": "json", "inc": "artist-credits+release-groups+media"},
            )
            if rel:
                await add_release(rel)

        if not out:
            q = f'release:"{group.album}"'
            if group.album_artist and group.album_artist != "?":
                q += f' AND artist:"{group.album_artist}"'
            data = await get_json(
                client, f"{MB}/release",
                params={"fmt": "json", "query": q, "limit": 8},
            )
            for rel in (data or {}).get("releases", [])[:8]:
                if int(rel.get("score", 0)) < 70:
                    continue
                full = await get_json(
                    client, f"{MB}/release/{rel['id']}",
                    params={"fmt": "json", "inc": "artist-credits+release-groups+media"},
                )
                if full:
                    await add_release(full)

        # release-group fallback image (lower trust)
        rg_id = group.release_group_id
        if not out and not rg_id:
            data = await get_json(
                client, f"{MB}/release-group",
                params={"fmt": "json",
                        "query": f'releasegroup:"{group.album}" AND artist:"{group.album_artist}"',
                        "limit": 3},
            )
            for rg in (data or {}).get("release-groups", [])[:3]:
                if int(rg.get("score", 0)) >= 80:
                    rg_id = rg["id"]
                    break
        if rg_id:
            info = await get_json(client, f"{CAA}/release-group/{rg_id}")
            if info and any(img.get("front") for img in info.get("images", [])):
                out.append(CandidateData(
                    source="musicbrainz",
                    image_url=_caa_front(rg_id, "release-group"),
                    provenance_url=f"https://musicbrainz.org/release-group/{rg_id}",
                    release=ReleaseMeta(
                        source="musicbrainz", title=group.album, artist=group.album_artist,
                        release_group_id=rg_id, is_release_group_only=True,
                    ),
                ))
        return out
