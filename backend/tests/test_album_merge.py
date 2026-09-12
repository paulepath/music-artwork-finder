from __future__ import annotations

import struct
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from mutagen.flac import FLAC
from sqlalchemy import select

from app.config import get_settings
from app.db import init_db, session_scope
from app.grouping import album_base, merge_candidate_key, suggest_merges
from app.main import app
from app.models import AlbumGroup, AlbumMerge, ArtworkQuery, GroupState, QueryRole, ScanRun, Track
from app.routers.albums import _summary
from app.routers.search import _resolve_tracks
from app.schemas import SearchSelectionRequest
from app.scanner import run_scan
from app.search_service import _album_meta, create_search_session


def _make_flac(path: Path, *, title: str, artist: str, album: str, album_artist: str = "", disc: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    min_block, max_block = 4096, 4096
    sample_rate, channels, bps, total_samples = 44100, 2, 16, 0
    b1 = struct.pack(">HH", min_block, max_block)
    b2 = struct.pack(">I", 0)[1:]
    b3 = struct.pack(">I", 0)[1:]
    val = (sample_rate << 44) | ((channels - 1) << 41) | ((bps - 1) << 36) | (total_samples & 0xFFFFFFFFF)
    b4 = struct.pack(">Q", val)
    b5 = b"\x00" * 16
    header = b"fLaC"
    mb_header = bytes([0x80, 0x00, 0x00, 34])
    path.write_bytes(header + mb_header + b1 + b2 + b3 + b4 + b5)
    f = FLAC(str(path))
    f["title"] = title
    f["artist"] = artist
    f["albumartist"] = album_artist or artist
    f["album"] = album
    f["discnumber"] = str(disc)
    f.save()


def test_suggest_merges_finds_cd1_cd2_sibling_pair():
    g1 = SimpleNamespace(
        id=1, album_artist="Pink Floyd", album="The Wall (Disc 1)",
        disc=1, common_dir="/music/Pink Floyd/The Wall/CD1", track_count=13,
    )
    g2 = SimpleNamespace(
        id=2, album_artist="Pink Floyd", album="The Wall (Disc 2)",
        disc=2, common_dir="/music/Pink Floyd/The Wall/CD2", track_count=13,
    )
    suggestions = suggest_merges([g1, g2])
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.title == "the wall"
    assert s.album_artist == "Pink Floyd"
    assert len(s) == 2
    assert s[0] == g1
    assert s[1] == g2


def test_suggest_merges_does_not_merge_different_albums_under_same_parent():
    g1 = SimpleNamespace(
        id=1, album_artist="Radiohead", album="OK Computer",
        disc=1, common_dir="/music/Radiohead/OK Computer", track_count=12,
    )
    g2 = SimpleNamespace(
        id=2, album_artist="Radiohead", album="Kid A",
        disc=1, common_dir="/music/Radiohead/Kid A", track_count=10,
    )
    suggestions = suggest_merges([g1, g2])
    assert len(suggestions) == 0


def test_suggest_merges_skips_already_merged_or_dismissed_groups():
    # Already merged pair
    g1 = SimpleNamespace(
        id=1, album_artist="Pink Floyd", album="The Wall (Disc 1)",
        disc=1, common_dir="/music/Pink Floyd/The Wall/CD1", track_count=13,
        merged_into_id=42,
    )
    g2 = SimpleNamespace(
        id=2, album_artist="Pink Floyd", album="The Wall (Disc 2)",
        disc=2, common_dir="/music/Pink Floyd/The Wall/CD2", track_count=13,
        merged_into_id=42,
    )
    assert suggest_merges([g1, g2]) == []

    # Dismissed member
    g3 = SimpleNamespace(
        id=3, album_artist="Pink Floyd", album="The Wall (Disc 1)",
        disc=1, common_dir="/music/Pink Floyd/The Wall/CD1", track_count=13,
        merge_dismissed=True,
    )
    g4 = SimpleNamespace(
        id=4, album_artist="Pink Floyd", album="The Wall (Disc 2)",
        disc=2, common_dir="/music/Pink Floyd/The Wall/CD2", track_count=13,
        merge_dismissed=False,
    )
    assert suggest_merges([g3, g4]) == []


def test_post_merges_rejects_mismatched_keys_and_invalid_payloads():
    init_db()
    token = uuid.uuid4().hex[:8]
    with session_scope() as db:
        g1 = AlbumGroup(group_key=f"k1-{token}", album="Album One", album_artist="Artist", common_dir=f"/{token}/A1")
        g2 = AlbumGroup(group_key=f"k2-{token}", album="Album Two", album_artist="Artist", common_dir=f"/{token}/A2")
        db.add_all([g1, g2])
        db.flush()
        id1, id2 = g1.id, g2.id

    with TestClient(app) as client:
        # Fewer than 2 groups
        res1 = client.post("/api/albums/merges", json={"group_ids": [id1]})
        assert res1.status_code == 422

        # Unknown group id
        res2 = client.post("/api/albums/merges", json={"group_ids": [id1, 999999]})
        assert res2.status_code == 422

        # Mismatched merge candidate keys are now accepted as manual merge
        res3 = client.post("/api/albums/merges", json={"group_ids": [id1, id2]})
        assert res3.status_code == 200


def test_merge_survives_run_scan():
    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root
    p1 = root / token / "Set" / "CD1" / "01.flac"
    p2 = root / token / "Set" / "CD2" / "01.flac"
    _make_flac(p1, title="Track 1", artist="Band", album="Set (Disc 1)", disc=1)
    _make_flac(p2, title="Track 2", artist="Band", album="Set (Disc 2)", disc=2)

    scan_id1 = run_scan()
    assert scan_id1 > 0

    with session_scope() as db:
        groups = list(db.scalars(select(AlbumGroup).where(AlbumGroup.album.like("Set%")).order_by(AlbumGroup.disc)))
        assert len(groups) == 2
        g1, g2 = groups[0], groups[1]
        mkey = merge_candidate_key(g1.album_artist, g1.album, g1.common_dir)
        merge = AlbumMerge(merge_key=mkey, title="Set", album_artist=g1.album_artist)
        db.add(merge)
        db.flush()
        g1.merged_into_id = merge.id
        g2.merged_into_id = merge.id
        merge_id = merge.id

    # Rescan
    scan_id2 = run_scan()
    assert scan_id2 > 0

    # Verify merged_into_id persisted untouched
    with session_scope() as db:
        groups = list(db.scalars(select(AlbumGroup).where(AlbumGroup.album.like("Set%")).order_by(AlbumGroup.disc)))
        assert len(groups) == 2
        assert groups[0].merged_into_id == merge_id
        assert groups[1].merged_into_id == merge_id
        persisted_merge = db.get(AlbumMerge, merge_id)
        assert persisted_merge is not None
        assert persisted_merge.title == "Set"


def test_merged_set_produces_one_album_query_with_summed_track_count():
    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root
    with session_scope() as db:
        merge = AlbumMerge(merge_key=f"merge-{token}", title="Greatest Hits", album_artist="The Artist")
        db.add(merge)
        db.flush()

        g1 = AlbumGroup(
            group_key=f"g1-{token}", album="Greatest Hits (Disc 1)", album_artist="The Artist",
            disc=1, common_dir=f"/{token}/Hits/CD1", track_count=3, merged_into_id=merge.id,
        )
        g2 = AlbumGroup(
            group_key=f"g2-{token}", album="Greatest Hits (Disc 2)", album_artist="The Artist",
            disc=2, common_dir=f"/{token}/Hits/CD2", track_count=4, merged_into_id=merge.id,
        )
        db.add_all([g1, g2])
        db.flush()

        tracks = []
        for i in range(1, 4):
            p = root / token / "Hits" / "CD1" / f"{i:02d}.mp3"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
            rel_path = "/" + p.relative_to(root).as_posix()
            t = Track(
                group_id=g1.id, path=rel_path,
                title=f"D1 T{i}", album="Greatest Hits (Disc 1)", artist="The Artist",
            )
            db.add(t)
            tracks.append(t)
        for i in range(1, 5):
            p = root / token / "Hits" / "CD2" / f"{i:02d}.mp3"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
            rel_path = "/" + p.relative_to(root).as_posix()
            t = Track(
                group_id=g2.id, path=rel_path,
                title=f"D2 T{i}", album="Greatest Hits (Disc 2)", artist="The Artist",
            )
            db.add(t)
            tracks.append(t)
        db.flush()

        session = create_search_session(db, tracks, selection_kind="tracks")
        album_queries = list(db.scalars(
            select(ArtworkQuery).where(ArtworkQuery.session_id == session.id, ArtworkQuery.role == QueryRole.album)
        ))
        assert len(album_queries) == 1
        aq = album_queries[0]
        assert aq.query_key == f"merge:{merge.id}"

        meta = _album_meta(db, aq)
        assert meta.track_count == 7  # 3 + 4
        assert len(meta.track_paths) == 7


def test_get_albums_group_by_merge_collapses_rows():
    init_db()
    token = uuid.uuid4().hex[:8]
    with session_scope() as db:
        scan = db.scalar(select(ScanRun).order_by(ScanRun.id.desc()).limit(1))
        if scan is None:
            scan = ScanRun()
            db.add(scan)
            db.flush()
        scan_id = scan.id

        merge = AlbumMerge(merge_key=f"merge-{token}", title="Anthology", album_artist="Anthology Artist")
        db.add(merge)
        db.flush()

        g1 = AlbumGroup(
            group_key=f"g1-{token}", album="Anthology (Disc 1)", album_artist="Anthology Artist",
            disc=1, common_dir=f"/{token}/Anthology/CD1", track_count=5, merged_into_id=merge.id,
            last_seen_scan_id=scan_id,
        )
        g2 = AlbumGroup(
            group_key=f"g2-{token}", album="Anthology (Disc 2)", album_artist="Anthology Artist",
            disc=2, common_dir=f"/{token}/Anthology/CD2", track_count=6, merged_into_id=merge.id,
            last_seen_scan_id=scan_id,
        )
        g3 = AlbumGroup(
            group_key=f"g3-{token}", album="Solo Album", album_artist="Anthology Artist",
            disc=1, common_dir=f"/{token}/Solo", track_count=4, merged_into_id=None,
            last_seen_scan_id=scan_id,
        )
        db.add_all([g1, g2, g3])
        db.flush()
        id1, id2, id3 = g1.id, g2.id, g3.id

    with TestClient(app) as client:
        # Default group_by_merge=False
        res_unmerged = client.get("/api/albums", params={"q": "Anthology Artist", "group_by_merge": "false"})
        assert res_unmerged.status_code == 200
        items_unmerged = res_unmerged.json()
        assert len(items_unmerged) == 3

        # group_by_merge=True
        res_merged = client.get("/api/albums", params={"q": "Anthology Artist", "group_by_merge": "true"})
        assert res_merged.status_code == 200
        items_merged = res_merged.json()
        assert len(items_merged) == 2

        # Find the collapsed anthology item
        anthology_item = next(i for i in items_merged if i["id"] == id1)
        assert anthology_item["track_count"] == 11  # 5 + 6
        assert anthology_item["discs"] == [1, 2]
        assert set(anthology_item["member_group_ids"]) == {id1, id2}
        assert anthology_item["album"] == "Anthology"


def test_merge_api_endpoints_full_lifecycle():
    init_db()
    token = uuid.uuid4().hex[:8]
    with session_scope() as db:
        scan = db.scalar(select(ScanRun).order_by(ScanRun.id.desc()).limit(1))
        if scan is None:
            scan = ScanRun()
            db.add(scan)
            db.flush()
        scan_id = scan.id

        g1 = AlbumGroup(
            group_key=f"l1-{token}", album="Live (CD 1)", album_artist="Rock Band",
            disc=1, common_dir=f"/{token}/Live/CD1", track_count=8,
            last_seen_scan_id=scan_id,
        )
        g2 = AlbumGroup(
            group_key=f"l2-{token}", album="Live (CD 2)", album_artist="Rock Band",
            disc=2, common_dir=f"/{token}/Live/CD2", track_count=8,
            last_seen_scan_id=scan_id,
        )
        db.add_all([g1, g2])
        db.flush()
        id1, id2 = g1.id, g2.id

    with TestClient(app) as client:
        # 1. Merge suggestions endpoint finds them
        res_sugg = client.get("/api/albums/merge-suggestions")
        assert res_sugg.status_code == 200
        suggs = [s for s in res_sugg.json() if any(g["id"] == id1 for g in s["groups"])]
        assert len(suggs) == 1
        assert suggs[0]["title"] == "live"

        # 2. Create merge
        res_merge = client.post("/api/albums/merges", json={"group_ids": [id1, id2], "title": "Live in Concert"})
        assert res_merge.status_code == 200
        merge_id = res_merge.json()["id"]
        assert res_merge.json()["title"] == "Live in Concert"

        # 3. Merge suggestions should no longer include them
        res_sugg2 = client.get("/api/albums/merge-suggestions")
        assert not any(g["id"] == id1 for s in res_sugg2.json() for g in s["groups"])

        # 4. Delete merge
        res_del = client.delete(f"/api/albums/merges/{merge_id}")
        assert res_del.status_code == 200
        assert set(res_del.json()["unmerged_group_ids"]) == {id1, id2}

        # 5. Suggestions find them again
        res_sugg3 = client.get("/api/albums/merge-suggestions")
        assert any(g["id"] == id1 for s in res_sugg3.json() for g in s["groups"])

        # 6. Dismiss suggestions
        res_dismiss = client.post("/api/albums/merge-suggestions/dismiss", json={"group_ids": [id1, id2]})
        assert res_dismiss.status_code == 200
        assert res_dismiss.json()["dismissed_count"] == 2

        # 7. Suggestions no longer include them
        res_sugg4 = client.get("/api/albums/merge-suggestions")
        assert not any(g["id"] == id1 for s in res_sugg4.json() for g in s["groups"])


def _bare_group(**kw) -> SimpleNamespace:
    base = dict(album="", album_artist="", common_dir="", disc=1, track_count=1,
                merged_into_id=None, merge_dismissed=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_untagged_folders_under_one_parent_are_never_suggested_as_one_album():
    """Albums with no title all normalise to the same "?" sentinel key.

    Without a real shared album title, a pile of unrelated rips sitting under one
    parent directory would be offered to the user as a single multi-disc set.
    """
    unrelated = [
        _bare_group(common_dir="/Unsorted/rip-a"),
        _bare_group(common_dir="/Unsorted/rip-b"),
        _bare_group(common_dir="/Unsorted/rip-c"),
    ]
    assert suggest_merges(unrelated) == []

    # an album_artist alone is not enough evidence either
    va = [
        _bare_group(album_artist="Various Artists", common_dir="/VA/one"),
        _bare_group(album_artist="Various Artists", common_dir="/VA/two"),
    ]
    assert suggest_merges(va) == []

    # but a genuine shared title across two disc folders still merges
    real = [
        _bare_group(album="Dreams 3", album_artist="Various Artists",
                    common_dir="/Cafe/Dreams 3 CD1", disc=1),
        _bare_group(album="Dreams 3", album_artist="Various Artists",
                    common_dir="/Cafe/Dreams 3 CD2", disc=2),
    ]
    assert len(suggest_merges(real)) == 1


def test_suggest_merges_flat_folder_ten_discs():
    """Build 10 groups in one dir tagged Disc 1 - ... to Disc 10 - ..., shared album_artist.
    Yields one merge suggestion covering all 10, proposed title defaulting to dir basename.
    """
    groups = [
        SimpleNamespace(
            id=i,
            album_artist="100 Classical Masterpieces",
            album=f"Disc {i} - {1685 + i * 20}-{1730 + i * 20}",
            disc=i,
            common_dir="/The Top 100 Masterpieces of Classical Music 1685-1928",
            track_count=8,
            merged_into_id=None,
            merge_dismissed=False,
        )
        for i in range(1, 11)
    ]
    suggestions = suggest_merges(groups)
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.title == "The Top 100 Masterpieces of Classical Music 1685-1928"
    assert s.album_artist == "100 Classical Masterpieces"
    assert len(s.groups) == 10
    assert {g.id for g in s.groups} == set(range(1, 11))


def test_flat_folder_merge_accepts_bare_disc_titles_in_both_spellings():
    """A flat rip usually tags each disc with nothing but the disc marker.

    `normalize` leaves "CD1" as one token but splits "CD 1" into two, and
    `album_base` of either is empty — so matching only the spaced form, or
    requiring a non-empty base, silently drops the commonest flat layout.
    """
    for titles in (("CD1", "CD2"), ("CD 1", "CD 2"), ("Disc 1", "Disc 2")):
        groups = [
            SimpleNamespace(
                id=index, album_artist="Some Band", album=title, disc=index,
                common_dir="/Some Band/Anthology", track_count=10,
                merged_into_id=None, merge_dismissed=False,
            )
            for index, title in enumerate(titles, start=1)
        ]
        suggestions = suggest_merges(groups)
        assert len(suggestions) == 1, titles
        assert len(suggestions[0].groups) == 2, titles
        assert suggestions[0].title == "Anthology", titles

    # still no merge when the titles carry no disc marker at all
    untagged = [
        SimpleNamespace(id=1, album_artist="Some Band", album="", disc=1,
                        common_dir="/Some Band/Anthology", track_count=10,
                        merged_into_id=None, merge_dismissed=False),
        SimpleNamespace(id=2, album_artist="Some Band", album="", disc=2,
                        common_dir="/Some Band/Anthology", track_count=10,
                        merged_into_id=None, merge_dismissed=False),
    ]
    assert suggest_merges(untagged) == []


def test_suggest_merges_two_different_albums_loose_in_one_folder_not_merged():
    """Two genuinely different albums loose in one folder (no disc markers) are not merged."""
    g1 = SimpleNamespace(
        id=1, album_artist="Radiohead", album="OK Computer",
        disc=1, common_dir="/music/Radiohead", track_count=12,
        merged_into_id=None, merge_dismissed=False,
    )
    g2 = SimpleNamespace(
        id=2, album_artist="Radiohead", album="Kid A",
        disc=1, common_dir="/music/Radiohead", track_count=10,
        merged_into_id=None, merge_dismissed=False,
    )
    assert suggest_merges([g1, g2]) == []


def test_suggest_merges_non_disc_group_not_swept_into_flat_folder_merge():
    """A group whose title has no disc marker is not swept into a flat-folder merge."""
    discs = [
        SimpleNamespace(
            id=i,
            album_artist="100 Classical Masterpieces",
            album=f"Disc {i} - {1600 + i}",
            disc=i,
            common_dir="/The Top 100 Masterpieces of Classical Music 1685-1928",
            track_count=8,
            merged_into_id=None,
            merge_dismissed=False,
        )
        for i in range(1, 4)
    ]
    stray = SimpleNamespace(
        id=99,
        album_artist="100 Classical Masterpieces",
        album="Bonus Album Unrelated",
        disc=1,
        common_dir="/The Top 100 Masterpieces of Classical Music 1685-1928",
        track_count=5,
        merged_into_id=None,
        merge_dismissed=False,
    )
    suggestions = suggest_merges(discs + [stray])
    assert len(suggestions) == 1
    s = suggestions[0]
    assert len(s.groups) == 3
    assert 99 not in {g.id for g in s.groups}


def test_manual_merge_unrelated_keys_and_undo():
    """Manual merge of two groups with unrelated merge keys is accepted, and DELETE unmerges them."""
    init_db()
    token = uuid.uuid4().hex[:8]
    with session_scope() as db:
        g1 = AlbumGroup(group_key=f"m1-{token}", album="Alpha", album_artist="Artist 1", common_dir=f"/{token}/A")
        g2 = AlbumGroup(group_key=f"m2-{token}", album="Beta", album_artist="Artist 2", common_dir=f"/{token}/B")
        db.add_all([g1, g2])
        db.flush()
        id1, id2 = g1.id, g2.id

    with TestClient(app) as client:
        res = client.post("/api/albums/merges", json={"group_ids": [id1, id2], "title": "Combined Alpha Beta"})
        assert res.status_code == 200
        merge_data = res.json()
        assert merge_data["title"] == "Combined Alpha Beta"
        merge_id = merge_data["id"]

        with session_scope() as db:
            grp1 = db.get(AlbumGroup, id1)
            grp2 = db.get(AlbumGroup, id2)
            assert grp1.merged_into_id == merge_id
            assert grp2.merged_into_id == merge_id

        # DELETE unmerges them
        del_res = client.delete(f"/api/albums/merges/{merge_id}")
        assert del_res.status_code == 200
        assert set(del_res.json()["unmerged_group_ids"]) == {id1, id2}

        with session_scope() as db:
            grp1 = db.get(AlbumGroup, id1)
            grp2 = db.get(AlbumGroup, id2)
            assert grp1.merged_into_id is None
            assert grp2.merged_into_id is None
            assert db.get(AlbumMerge, merge_id) is None


def test_get_albums_collapses_by_default():
    """GET /api/albums collapses a merged set by default without passing group_by_merge=true."""
    init_db()
    token = uuid.uuid4().hex[:8]
    with session_scope() as db:
        scan = db.scalar(select(ScanRun).order_by(ScanRun.id.desc()).limit(1))
        if scan is None:
            scan = ScanRun()
            db.add(scan)
            db.flush()
        scan_id = scan.id

        merge = AlbumMerge(merge_key=f"merge-def-{token}", title="Default Collapse", album_artist="Artist")
        db.add(merge)
        db.flush()

        g1 = AlbumGroup(
            group_key=f"dc1-{token}", album="Default Collapse Part 1", album_artist="Artist",
            disc=1, common_dir=f"/{token}/DC/1", track_count=3, merged_into_id=merge.id,
            last_seen_scan_id=scan_id,
        )
        g2 = AlbumGroup(
            group_key=f"dc2-{token}", album="Default Collapse Part 2", album_artist="Artist",
            disc=2, common_dir=f"/{token}/DC/2", track_count=4, merged_into_id=merge.id,
            last_seen_scan_id=scan_id,
        )
        db.add_all([g1, g2])
        db.flush()
        id1, id2 = g1.id, g2.id

    with TestClient(app) as client:
        # Call without group_by_merge parameter (defaults to True)
        res = client.get("/api/albums", params={"q": "Default Collapse"})
        assert res.status_code == 200
        items = res.json()
        assert len(items) == 1
        item = items[0]
        assert item["album"] == "Default Collapse"
        assert item["track_count"] == 7
        assert item["discs"] == [1, 2]
        assert set(item["member_group_ids"]) == {id1, id2}


def test_merged_album_tracks_ordered_and_detail():
    """A merged album's tracks come back ordered (disc, track_no, path) with correct discs and summed track_count."""
    init_db()
    token = uuid.uuid4().hex[:8]
    with session_scope() as db:
        merge = AlbumMerge(merge_key=f"order-{token}", title="Ordered Set", album_artist="Band")
        db.add(merge)
        db.flush()

        g1 = AlbumGroup(
            group_key=f"og1-{token}", album="Ordered Set (Disc 1)", album_artist="Band",
            disc=1, common_dir=f"/{token}/Ordered/CD1", track_count=2, merged_into_id=merge.id,
        )
        g2 = AlbumGroup(
            group_key=f"og2-{token}", album="Ordered Set (Disc 2)", album_artist="Band",
            disc=2, common_dir=f"/{token}/Ordered/CD2", track_count=2, merged_into_id=merge.id,
        )
        db.add_all([g1, g2])
        db.flush()

        # Add tracks out of order on both discs
        t2_1 = Track(group_id=g2.id, path=f"/{token}/Ordered/CD2/02.mp3", title="D2 T2", track_no=2, disc=2)
        t1_2 = Track(group_id=g1.id, path=f"/{token}/Ordered/CD1/02.mp3", title="D1 T2", track_no=2, disc=1)
        t2_0 = Track(group_id=g2.id, path=f"/{token}/Ordered/CD2/01.mp3", title="D2 T1", track_no=1, disc=2)
        t1_1 = Track(group_id=g1.id, path=f"/{token}/Ordered/CD1/01.mp3", title="D1 T1", track_no=1, disc=1)
        db.add_all([t2_1, t1_2, t2_0, t1_1])
        db.flush()
        g1_id = g1.id

    with TestClient(app) as client:
        res = client.get(f"/api/albums/{g1_id}")
        assert res.status_code == 200
        data = res.json()
        assert data["album"] == "Ordered Set"
        assert data["discs"] == [1, 2]
        assert data["track_count"] == 4
        tracks = data["tracks"]
        assert len(tracks) == 4
        # Verify ordering: (disc, track_no, path)
        assert [(t["disc"], t["track_no"], t["path"]) for t in tracks] == [
            (1, 1, f"/{token}/Ordered/CD1/01.mp3"),
            (1, 2, f"/{token}/Ordered/CD1/02.mp3"),
            (2, 1, f"/{token}/Ordered/CD2/01.mp3"),
            (2, 2, f"/{token}/Ordered/CD2/02.mp3"),
        ]


def test_resolve_tracks_refuses_multi_album_parent_with_422():
    """_resolve_tracks refuses an untitled loose group with 422."""
    init_db()
    token = uuid.uuid4().hex[:8]
    with session_scope() as db:
        g = AlbumGroup(
            group_key=f"map-{token}", album="", album_artist="Various",
            common_dir=f"/{token}/ParentFolder", state=GroupState.multi_album_parent,
        )
        db.add(g)
        db.flush()
        gid = g.id

        body = SearchSelectionRequest(album_ids=[gid])
        with pytest.raises(HTTPException) as exc_info:
            _resolve_tracks(db, body)
        assert exc_info.value.status_code == 422
        assert f"/{token}/ParentFolder" in exc_info.value.detail


def test_resolve_tracks_allows_named_album_in_multi_album_parent():
    """_resolve_tracks allows albums in multi_album_parent when album tag is present."""
    init_db()
    token = uuid.uuid4().hex[:8]
    with session_scope() as db:
        g = AlbumGroup(
            group_key=f"map-{token}", album="Real Album", album_artist="Real Artist",
            common_dir=f"/{token}/ParentFolder", state=GroupState.multi_album_parent,
        )
        db.add(g)
        db.flush()
        t = Track(group_id=g.id, path=f"/{token}/ParentFolder/01.mp3", title="T1",
                  artist="Real Artist", album="Real Album", album_artist="Real Artist")
        db.add(t)
        db.flush()

        body = SearchSelectionRequest(album_ids=[g.id])
        tracks, kind, _ = _resolve_tracks(db, body)
        assert len(tracks) == 1
        assert kind == "albums"


def test_loose_tracks_flag():
    """AlbumSummary.is_loose_tracks is True when state is multi_album_parent and album tag is empty."""
    init_db()
    token = uuid.uuid4().hex[:8]
    with session_scope() as db:
        g_loose = AlbumGroup(
            group_key=f"loose-{token}", album="", album_artist="Various",
            common_dir="/Random", state=GroupState.multi_album_parent,
        )
        g_named = AlbumGroup(
            group_key=f"named-{token}", album="Some Compilation", album_artist="Various",
            common_dir="/Random", state=GroupState.multi_album_parent,
        )
        g_normal = AlbumGroup(
            group_key=f"normal-{token}", album="", album_artist="Artist",
            common_dir="/Album", state=GroupState.scanned,
        )
        db.add_all([g_loose, g_named, g_normal])
        db.flush()

        s_loose = _summary(g_loose)
        assert s_loose.is_loose_tracks is True

        s_named = _summary(g_named)
        assert s_named.is_loose_tracks is False

        s_normal = _summary(g_normal)
        assert s_normal.is_loose_tracks is False


def test_search_session_albums_tracks_ordered_by_disc_track_no_path():
    """GET /api/search-sessions/{session_id}/albums returns tracks[] ordered by (disc, track_no, path)."""
    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root
    with session_scope() as db:
        g = AlbumGroup(
            group_key=f"sa-{token}", album="Session Album", album_artist="Artist",
            disc=1, common_dir=f"/{token}/SA", track_count=3,
        )
        db.add(g)
        db.flush()

        p3 = root / token / "SA" / "03.flac"
        p1 = root / token / "SA" / "01.flac"
        p2 = root / token / "SA" / "02.flac"
        _make_flac(p3, title="T3", artist="Artist", album="Session Album", disc=2)
        _make_flac(p1, title="T1", artist="Artist", album="Session Album", disc=1)
        _make_flac(p2, title="T2", artist="Artist", album="Session Album", disc=1)

        t3 = Track(group_id=g.id, path="/" + p3.relative_to(root).as_posix(), title="T3", album="Session Album", album_artist="Artist", track_no=3, disc=2)
        t1 = Track(group_id=g.id, path="/" + p1.relative_to(root).as_posix(), title="T1", album="Session Album", album_artist="Artist", track_no=1, disc=1)
        t2 = Track(group_id=g.id, path="/" + p2.relative_to(root).as_posix(), title="T2", album="Session Album", album_artist="Artist", track_no=2, disc=1)
        db.add_all([t3, t1, t2])
        db.flush()

        session = create_search_session(db, [t3, t1, t2], selection_kind="tracks")
        sid = session.id

    with TestClient(app) as client:
        res = client.get(f"/api/search-sessions/{sid}/albums")
        assert res.status_code == 200
        data = res.json()
        assert len(data["albums"]) == 1
        alb = data["albums"][0]
        assert alb["discs"] == [1, 2]
        tracks = alb["tracks"]
        assert len(tracks) == 3
        assert [(t["disc"], t["track_no"]) for t in tracks] == [
            (1, 1),
            (1, 2),
            (2, 3),
        ]
