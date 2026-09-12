"""Album identification for untagged folders via MusicBrainz."""
from __future__ import annotations

import logging
import posixpath
import re
from typing import Any

import httpx

from .http import get_json
from .matching import normalize, token_similarity

log = logging.getLogger("artwork.identify")

MB = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org"

_YEAR_PREFIX = re.compile(r"^\s*[\(\[]\s*(\d{4})\s*[\)\]]\s*[-_ ]*\s*")
_DISC_SUBDIR = re.compile(r"^(?:cd|disc|disk)\s*\d+$", re.IGNORECASE)


def guess_album_title(common_dir: str) -> str:
    """Derive an album title guess from a folder path, e.g. '/Minecraft_OST' -> 'Minecraft OST'."""
    if not common_dir:
        return ""
    p = posixpath.normpath(common_dir.replace("\\", "/")).strip("/")
    if not p or p in (".", "/"):
        return ""
    basename = posixpath.basename(p)
    if _DISC_SUBDIR.match(basename):
        parent = posixpath.dirname(p)
        if parent and parent not in (".", "/"):
            basename = posixpath.basename(parent)
    cleaned = _YEAR_PREFIX.sub("", basename)
    cleaned = cleaned.replace("_", " ")
    return " ".join(cleaned.split())


def _artist_credit(item: dict) -> str:
    return "".join(
        part.get("name", "") + part.get("joinphrase", "")
        for part in (item.get("artist-credit") or [])
    )


def score_release(
    rel: dict,
    *,
    guess: str,
    local_titles: list[str],
    durations: list[float | None] | None = None,
    track_count: int = 0,
) -> dict[str, Any]:
    """Score a candidate release against local track list and guess.

    Match titles as a set/multiset, never positionally, since track_no is often None.
    """
    rel_id = rel.get("id", "")
    rel_title = rel.get("title", "")
    artist = _artist_credit(rel)
    rel_group = rel.get("release-group") or {}
    release_group_id = rel_group.get("id") or ""
    date = rel.get("date") or rel_group.get("first-release-date") or ""
    year = int(date[:4]) if date[:4].isdigit() else None

    # Collect recordings across all media
    mb_titles: list[str] = []
    mb_durations: list[float | None] = []
    for medium in rel.get("media", []):
        for track in medium.get("tracks", []):
            mb_titles.append(track.get("title", ""))
            length_ms = track.get("length")
            mb_durations.append((length_ms / 1000.0) if length_ms else None)

    mb_track_count = len(mb_titles)
    norm_local = [normalize(t) for t in local_titles if (t or "").strip()]
    norm_mb = [normalize(t) for t in mb_titles if (t or "").strip()]

    matched_titles = 0
    available_mb = list(norm_mb)
    for lt in norm_local:
        best_match_idx = -1
        best_sim = 0.0
        for idx, mt in enumerate(available_mb):
            if lt == mt:
                best_sim = 1.0
                best_match_idx = idx
                break
            sim = token_similarity(lt, mt)
            if sim > best_sim:
                best_sim = sim
                best_match_idx = idx
        if best_sim >= 0.7 and best_match_idx >= 0:
            matched_titles += 1
            available_mb.pop(best_match_idx)

    total_local = max(1, len(norm_local))
    title_match_ratio = matched_titles / total_local

    # Track count similarity
    target_count = track_count or len(local_titles)
    if target_count and mb_track_count:
        count_ratio = min(target_count, mb_track_count) / max(target_count, mb_track_count)
    else:
        count_ratio = 0.5

    # Title guess similarity
    guess_sim = token_similarity(normalize(guess), normalize(rel_title)) if guess else 0.5

    # Score calculation: title match is strongest signal
    score = round(title_match_ratio * 0.65 + count_ratio * 0.20 + guess_sim * 0.15, 3)

    return {
        "mbid": rel_id,
        "release_group_id": release_group_id,
        "title": rel_title,
        "artist": artist,
        "year": year,
        "track_count": mb_track_count,
        "score": score,
        "matched_titles": matched_titles,
        "total_titles": len(local_titles),
        "cover_url": f"{CAA}/release/{rel_id}/front-500" if rel_id else None,
    }


async def identify_album(
    client: httpx.AsyncClient,
    *,
    guess: str,
    local_titles: list[str],
    durations: list[float | None] | None = None,
    track_count: int = 0,
    artist_hint: str = "",
) -> list[dict[str, Any]]:
    """Identify an album from guess + local tracks using MusicBrainz.

    Hard constraints:
    - Sample only 3-5 tracks for Seed B, never the whole folder.
    - Cap total candidate release fetches (~8 max) to respect rate limits.
    """
    seed_a_ids: list[str] = []

    # Seed A: Query release by guess title
    if guess:
        q = f'release:"{guess}"'
        if artist_hint:
            q += f' AND artist:"{artist_hint}"'
        try:
            data = await get_json(client, f"{MB}/release", params={"fmt": "json", "query": q, "limit": 8})
            for rel in (data or {}).get("releases", [])[:8]:
                rid = rel.get("id")
                if rid and rid not in seed_a_ids:
                    seed_a_ids.append(rid)
        except Exception as exc:
            log.warning("Seed A release query failed: %s", exc)

    # Seed B: Sample 3-5 tracks only
    non_empty_titles = [t.strip() for t in local_titles if t and t.strip()]
    if len(non_empty_titles) <= 5:
        sample = non_empty_titles
    else:
        indices = [int(i * (len(non_empty_titles) - 1) / 4) for i in range(5)]
        sample = [non_empty_titles[i] for i in sorted(set(indices))]

    seed_b_counts: dict[str, int] = {}
    seed_b_ids: list[str] = []

    for title in sample:
        q = f'recording:"{title}"'
        if artist_hint:
            q += f' AND artist:"{artist_hint}"'
        try:
            data = await get_json(client, f"{MB}/recording", params={"fmt": "json", "query": q, "limit": 5})
            for recording in (data or {}).get("recordings", []):
                if int(recording.get("score", 0)) >= 70:
                    for rel in (recording.get("releases") or [])[:5]:
                        rid = rel.get("id")
                        if rid:
                            seed_b_counts[rid] = seed_b_counts.get(rid, 0) + 1
                            if rid not in seed_b_ids:
                                seed_b_ids.append(rid)
        except Exception as exc:
            log.warning("Seed B recording query for %r failed: %s", title, exc)

    # Combine candidates:
    # 1. Releases found in BOTH Seed A and Seed B (highest confidence)
    # 2. Releases found in Seed B (corroborated by actual tracks), sorted by match frequency
    # 3. Releases found in Seed A only
    both = [rid for rid in seed_b_ids if rid in seed_a_ids]
    both.sort(key=lambda rid: seed_b_counts.get(rid, 0), reverse=True)

    seed_b_only = [rid for rid in seed_b_ids if rid not in seed_a_ids]
    seed_b_only.sort(key=lambda rid: seed_b_counts.get(rid, 0), reverse=True)

    seed_a_only = [rid for rid in seed_a_ids if rid not in seed_b_ids]

    combined_ids: list[str] = []
    for rid in both + seed_b_only + seed_a_only:
        if rid not in combined_ids:
            combined_ids.append(rid)

    # Cap release lookups to at most 8 candidate releases
    capped_ids = combined_ids[:8]
    candidates: list[dict[str, Any]] = []

    for rid in capped_ids:
        try:
            rel = await get_json(
                client,
                f"{MB}/release/{rid}",
                params={"fmt": "json", "inc": "artist-credits+release-groups+media+recordings"},
            )
            if not rel or not isinstance(rel, dict):
                continue
            scored = score_release(
                rel,
                guess=guess,
                local_titles=local_titles,
                durations=durations,
                track_count=track_count,
            )
            candidates.append(scored)
        except Exception as exc:
            log.warning("Fetching release %s failed: %s", rid, exc)

    # Sort candidates by score descending
    candidates.sort(key=lambda c: (c["score"], c["matched_titles"]), reverse=True)
    return candidates
