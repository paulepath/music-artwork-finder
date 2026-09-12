"""Normalisation + confidence-tier logic.

The regression this module exists to prevent: an album tagged ``Café Del Mar Vol. 3``
must never accept a cover whose release title is ``'70s Greatest Hits``.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .models import Tier

_PAREN_NOISE = re.compile(
    r"\((?:[^()]*\b(?:remaster(?:ed)?|deluxe|expanded|edition|version|mono|stereo|"
    r"bonus|reissue|explicit|clean|disc\s*\d+|cd\s*\d+|anniversary)\b[^()]*)\)",
    re.IGNORECASE,
)
_BRACKET_NOISE = re.compile(r"\[[^\[\]]*\]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_ROMAN = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
    "ix": 9, "x": 10,
}
_VA_TOKENS = {"various", "variousartists", "va", "varios", "verschiedene"}


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize(s: str | None) -> str:
    if not s:
        return ""
    s = strip_accents(s).lower()
    s = _PAREN_NOISE.sub(" ", s)
    s = _BRACKET_NOISE.sub(" ", s)
    s = s.replace("&", " and ")
    s = _NON_ALNUM.sub(" ", s)
    return " ".join(s.split())


def _volume_tokens(norm: str) -> set[str]:
    """Extract normalised volume/part numbers, mapping roman numerals + words.

    ``cafe del mar vol 3`` and ``cafe del mar volumen tres`` both -> {"3"}.
    """
    out: set[str] = set()
    words = norm.split()
    number_words = {
        "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
        "seven": "7", "eight": "8", "nine": "9", "ten": "10",
        "uno": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5",
    }
    for i, w in enumerate(words):
        if w in ("vol", "volume", "volumen", "part", "pt", "parte", "no", "nr"):
            if i + 1 < len(words):
                nxt = words[i + 1]
                if nxt.isdigit():
                    out.add(str(int(nxt)))
                elif nxt in _ROMAN:
                    out.add(str(_ROMAN[nxt]))
                elif nxt in number_words:
                    out.add(number_words[nxt])
        if w.isdigit():
            out.add(str(int(w)))
        elif w in _ROMAN and len(words) <= 6:
            out.add(str(_ROMAN[w]))
    return out


def is_various_artists(name: str | None) -> bool:
    return normalize(name).replace(" ", "") in _VA_TOKENS


def token_similarity(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class ReleaseMeta:
    """What a source reports about a candidate release."""
    source: str
    title: str
    artist: str
    year: int | None = None
    track_count: int | None = None
    mbid: str | None = None
    release_group_id: str | None = None
    barcode: str | None = None
    is_release_group_only: bool = False


@dataclass
class GroupMeta:
    """What we know about the local album group."""
    album: str
    album_artist: str
    year: int | None
    track_count: int
    mbid: str | None
    release_group_id: str | None
    is_compilation: bool
    track_paths: tuple[str, ...] = ()


@dataclass
class TrackMeta:
    """Metadata used to find a single/track release image."""
    title: str
    artist: str
    album: str = ""
    year: int | None = None
    musicbrainz_trackid: str | None = None


@dataclass
class TierResult:
    tier: Tier
    confidence: float
    reason: str
    rejected: bool = False


def classify(group: GroupMeta, rel: ReleaseMeta) -> TierResult:
    """Decide how far we trust ``rel`` as the cover for ``group``."""
    g_album, r_album = normalize(group.album), normalize(rel.title)
    g_artist, r_artist = normalize(group.album_artist), normalize(rel.artist)

    mbid_match = bool(group.mbid and rel.mbid and group.mbid == rel.mbid)
    title_sim = token_similarity(g_album, r_album)
    artist_sim = token_similarity(g_artist, r_artist)

    # --- hard rejections (apply even to MBID matches: a wrong MBID in the local
    #     tags must not become a bulk-appliable "exact" cover) ------------------
    g_vols, r_vols = _volume_tokens(g_album), _volume_tokens(r_album)
    if g_vols and r_vols and g_vols.isdisjoint(r_vols):
        return TierResult(Tier.fuzzy, 0.0,
                          f"volume mismatch: local {sorted(g_vols)} vs candidate {sorted(r_vols)}",
                          rejected=True)
    if title_sim < 0.34 and not mbid_match:
        return TierResult(Tier.fuzzy, title_sim,
                          f"album title too different ('{rel.title}' vs '{group.album}')",
                          rejected=True)

    # --- exact identifier match ------------------------------------------------
    if mbid_match:
        # sanity-check the candidate's own metadata; a stored MBID pointing at an
        # unrelated release drops to manual review instead of auto-apply.
        artist_ok_for_mbid = (
            artist_sim >= 0.6
            or (group.is_compilation and is_various_artists(rel.artist))
            or is_various_artists(group.album_artist)
        )
        if title_sim >= 0.8 and artist_ok_for_mbid:
            return TierResult(Tier.exact, 1.0, "MusicBrainz release MBID matches exactly")
        return TierResult(Tier.fuzzy, 0.4,
                          f"local MBID matches but candidate metadata looks unrelated "
                          f"(title~{title_sim:.2f}, artist~{artist_sim:.2f}) — review manually")

    # --- strong: names + track count + year all line up -------------------
    # NOTE: do not bypass artist checking just because the *local* file is
    # tagged Various Artists — that describes nearly every track in a VA
    # compilation library and would let any title-only match through
    # regardless of who the candidate release is actually by. Only trust a
    # VA/compiler-name mismatch when the *candidate* also looks like a VA
    # compilation (both sides agree it's a compilation, not just ours).
    artist_ok = (
        token_similarity(g_artist, r_artist) >= 0.6
        or (group.is_compilation and is_various_artists(rel.artist))
    )
    tc_ok = (
        rel.track_count is not None
        and group.track_count > 0
        and abs(rel.track_count - group.track_count) <= 1
    )
    year_ok = (
        group.year is None or rel.year is None or abs(group.year - rel.year) <= 1
    )
    vol_ok = not g_vols or not r_vols or not g_vols.isdisjoint(r_vols)

    if title_sim >= 0.8 and artist_ok and tc_ok and year_ok and vol_ok and not rel.is_release_group_only:
        conf = 0.9 + 0.1 * title_sim
        return TierResult(Tier.strong, min(conf, 0.99),
                          "album+artist match with matching track count"
                          + ("" if group.year is None else " and year"))

    if rel.is_release_group_only and title_sim >= 0.85 and artist_ok:
        return TierResult(Tier.strong, 0.82,
                          "release-group image; title+artist match (no exact release)")

    # --- fuzzy: needs a human --------------------------------------------
    parts = [f"title~{title_sim:.2f}"]
    if not artist_ok:
        parts.append("artist differs")
    if not tc_ok:
        parts.append("track count unverified")
    if not year_ok:
        parts.append("year differs")
    conf = 0.4 * title_sim + (0.2 if artist_ok else 0) + (0.2 if tc_ok else 0)
    return TierResult(Tier.fuzzy, round(conf, 3), "; ".join(parts))


def classify_track(track: TrackMeta, *, title: str, artist: str,
                   exact_identifier: bool = False) -> TierResult:
    """Score a provider's track hit without silently accepting a title collision."""
    local_title, remote_title = normalize(track.title), normalize(title)
    local_artist, remote_artist = normalize(track.artist), normalize(artist)
    title_sim = token_similarity(local_title, remote_title)
    artist_sim = token_similarity(local_artist, remote_artist)
    if exact_identifier and title_sim >= 0.6:
        return TierResult(Tier.exact, 1.0, "MusicBrainz track identifier matches")
    if title_sim < 0.5:
        return TierResult(Tier.fuzzy, title_sim,
                          f"track title too different ('{title}' vs '{track.title}')",
                          rejected=True)
    if local_artist and artist_sim < 0.34:
        return TierResult(Tier.fuzzy, artist_sim,
                          f"track artist differs ('{artist}' vs '{track.artist}')",
                          rejected=True)
    if title_sim >= 0.85 and (not local_artist or artist_sim >= 0.6):
        return TierResult(Tier.strong, min(0.99, 0.75 + 0.15 * title_sim + 0.1 * artist_sim),
                          "track title and artist match")
    confidence = round(0.65 * title_sim + 0.35 * artist_sim, 3)
    return TierResult(Tier.fuzzy, confidence,
                      f"track title~{title_sim:.2f}; artist~{artist_sim:.2f}")
