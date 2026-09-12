"""The incident this whole app exists to prevent.

MA once showed *Café del Mar Vol. 3* with a *'70s Greatest Hits* cover after a beets
bulk import. That must never be an appliable candidate again.
"""
from __future__ import annotations

from app.matching import GroupMeta, ReleaseMeta, classify
from app.models import Tier


def _cafe_group() -> GroupMeta:
    return GroupMeta(
        album="Café Del Mar Vol. 3", album_artist="Café Del Mar", year=1996,
        track_count=13, mbid=None, release_group_id=None, is_compilation=True,
    )


def test_cafe_del_mar_vol3_never_resolves_to_70s_greatest_hits():
    bad = ReleaseMeta(source="itunes", title="'70s Greatest Hits", artist="Various Artists",
                      year=1996, track_count=13)
    res = classify(_cafe_group(), bad)
    assert res.rejected is True
    assert res.tier != Tier.exact and res.tier != Tier.strong


def test_volume_mismatch_is_rejected():
    other_vol = ReleaseMeta(source="deezer", title="Café del Mar, Vol. 7", artist="Café del Mar",
                            year=2000, track_count=13)
    assert classify(_cafe_group(), other_vol).rejected is True


def test_spanish_volume_title_still_matches_volume_three():
    correct = ReleaseMeta(
        source="musicbrainz", title="Café del Mar - Ibiza Volumen Tres",
        artist="Café del Mar", year=1996, track_count=13,
    )
    res = classify(_cafe_group(), correct)
    assert res.rejected is False
    assert res.tier in (Tier.strong, Tier.fuzzy)  # not rejected; human/auto can take it
