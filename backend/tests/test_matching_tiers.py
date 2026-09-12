from __future__ import annotations

from app.matching import GroupMeta, ReleaseMeta, classify, normalize
from app.models import Tier


def _group(**kw) -> GroupMeta:
    base = dict(album="", album_artist="", year=None, track_count=0, mbid=None,
                release_group_id=None, is_compilation=False)
    base.update(kw)
    return GroupMeta(**base)


def test_exact_mbid_match_is_exact_tier():
    g = _group(album="Random Access Memories", album_artist="Daft Punk",
               track_count=13, mbid="abc-123")
    r = ReleaseMeta(source="musicbrainz", title="Random Access Memories", artist="Daft Punk",
                    track_count=13, mbid="abc-123")
    res = classify(g, r)
    assert res.tier == Tier.exact and res.confidence == 1.0


def test_full_metadata_match_is_strong():
    g = _group(album="Greatest Hits II", album_artist="Queen", year=1991, track_count=17)
    r = ReleaseMeta(source="itunes", title="Greatest Hits II", artist="Queen",
                    year=1991, track_count=17)
    assert classify(g, r).tier == Tier.strong


def test_track_count_mismatch_drops_to_fuzzy():
    g = _group(album="Buddha-Bar III", album_artist="Various Artists", track_count=26,
               is_compilation=True)
    r = ReleaseMeta(source="deezer", title="Buddha Bar III", artist="Various Artists",
                    track_count=12)
    assert classify(g, r).tier == Tier.fuzzy


def test_normalize_handles_accents_and_noise():
    assert normalize("Café del Mar (Remastered) [Ambient]") == "cafe del mar"
