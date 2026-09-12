from __future__ import annotations

import hashlib
import struct
import uuid
from pathlib import Path

import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4, MP4Cover
from sqlalchemy import select

from app.config import get_settings
from app.db import init_db, session_scope
from app.dual_writer import (
    TRACK_DESCRIPTION,
    _write_roles,
    apply_track_artwork,
)
from app.models import (
    AlbumGroup,
    ArtworkAsset,
    ArtworkCluster,
    ArtworkQuery,
    QueryRole,
    SearchSession,
    SearchTarget,
    Track,
    TrackWriteAudit,
    WorkStatus,
)
from app.search_service import apply_search_session, create_search_session
from tests.test_search_v2 import _jpeg, _tagged_mp3


def _make_flac(path: Path) -> None:
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


def _make_m4a(path: Path) -> None:
    ftyp_data = b"M4A \x00\x00\x02\x00M4A mp42isom"
    ftyp = struct.pack(">I", len(ftyp_data) + 8) + b"ftyp" + ftyp_data
    moov = struct.pack(">I", 8) + b"moov"
    path.write_bytes(ftyp + moov)


def test_mp3_single_cover_write_preserves_unrelated_and_enforces_v23(tmp_path):
    path = tmp_path / "song.mp3"
    path.touch()

    front = _jpeg((255, 0, 0))
    legacy_track = _jpeg((0, 255, 0))
    unrelated_booklet = _jpeg((0, 0, 255))
    new_cover = _jpeg((200, 200, 200))

    tags = ID3()
    tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Album Cover", data=front))
    tags.add(APIC(encoding=3, mime="image/jpeg", type=0, desc=TRACK_DESCRIPTION, data=legacy_track))
    tags.add(APIC(encoding=3, mime="image/jpeg", type=5, desc="Booklet", data=unrelated_booklet))
    tags.save(path)

    _write_roles(path, album=new_cover, track_art=None, replace_album=True, replace_track=True)

    reloaded = ID3(path)
    pics = reloaded.getall("APIC")

    # Assert exactly one front cover
    front_pics = [p for p in pics if p.type == 3]
    assert len(front_pics) == 1
    assert bytes(front_pics[0].data) == new_cover

    # Assert no "Track Artwork" frame remains
    assert not any(p.type == 0 and p.desc == TRACK_DESCRIPTION for p in pics)
    assert not any(bytes(p.data) == legacy_track for p in pics)

    # Assert unrelated picture is preserved
    unrelated_pics = [p for p in pics if p.type == 5]
    assert len(unrelated_pics) == 1
    assert bytes(unrelated_pics[0].data) == unrelated_booklet

    # Assert ID3 version is v2.3
    assert reloaded.version == (2, 3, 0)


def test_flac_single_cover_write_preserves_unrelated(tmp_path):
    path = tmp_path / "song.flac"
    _make_flac(path)

    front = _jpeg((255, 0, 0))
    legacy_track = _jpeg((0, 255, 0))
    unrelated_booklet = _jpeg((0, 0, 255))
    new_cover = _jpeg((150, 150, 150))

    audio = FLAC(path)
    p_front = Picture()
    p_front.type = 3
    p_front.mime = "image/jpeg"
    p_front.desc = "Album Cover"
    p_front.data = front

    p_track = Picture()
    p_track.type = 0
    p_track.mime = "image/jpeg"
    p_track.desc = TRACK_DESCRIPTION
    p_track.data = legacy_track

    p_booklet = Picture()
    p_booklet.type = 5
    p_booklet.mime = "image/jpeg"
    p_booklet.desc = "Booklet"
    p_booklet.data = unrelated_booklet

    audio.add_picture(p_front)
    audio.add_picture(p_track)
    audio.add_picture(p_booklet)
    audio.save()

    _write_roles(path, album=new_cover, track_art=None, replace_album=True, replace_track=True)

    reloaded = FLAC(path)
    pics = reloaded.pictures

    # Exactly one front cover
    front_pics = [p for p in pics if p.type == 3]
    assert len(front_pics) == 1
    assert bytes(front_pics[0].data) == new_cover

    # No "Track Artwork" frame remains
    assert not any(p.type == 0 and p.desc == TRACK_DESCRIPTION for p in pics)
    assert not any(bytes(p.data) == legacy_track for p in pics)

    # Unrelated picture preserved
    booklet_pics = [p for p in pics if p.type == 5]
    assert len(booklet_pics) == 1
    assert bytes(booklet_pics[0].data) == unrelated_booklet


def test_m4a_single_cover_write_preserves_unrelated(tmp_path):
    path = tmp_path / "song.m4a"
    _make_m4a(path)

    front = _jpeg((255, 0, 0))
    legacy_track = _jpeg((0, 255, 0))
    unrelated_booklet = _jpeg((0, 0, 255))
    new_cover = _jpeg((120, 120, 120))

    audio = MP4(path)
    audio.add_tags()
    audio["covr"] = [
        MP4Cover(front, imageformat=MP4Cover.FORMAT_JPEG),
        MP4Cover(legacy_track, imageformat=MP4Cover.FORMAT_JPEG),
        MP4Cover(unrelated_booklet, imageformat=MP4Cover.FORMAT_JPEG),
    ]
    audio.save()

    _write_roles(path, album=new_cover, track_art=None, replace_album=True, replace_track=True)

    reloaded = MP4(path)
    covrs = [bytes(c) for c in (reloaded.tags or {}).get("covr", [])]

    # Exactly one front cover at index 0
    assert len(covrs) == 2
    assert covrs[0] == new_cover

    # Legacy track artwork is gone
    assert legacy_track not in covrs

    # Unrelated picture preserved
    assert covrs[1] == unrelated_booklet


def test_apply_search_session_writes_single_cover_and_audit(monkeypatch):
    """End-to-end test of apply_search_session with governing role and audit logging."""
    import app.search_service as service

    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root
    dir_path = root / token / "Album"
    dir_path.mkdir(parents=True, exist_ok=True)
    mp3_path = dir_path / "01.mp3"
    _tagged_mp3(mp3_path, title="First", artist="Artist", album="Album")

    # Add legacy track art and booklet to the file
    legacy_art = _jpeg((10, 20, 30))
    booklet_art = _jpeg((40, 50, 60))
    tags = ID3(mp3_path)
    tags.add(APIC(encoding=3, mime="image/jpeg", type=0, desc=TRACK_DESCRIPTION, data=legacy_art))
    tags.add(APIC(encoding=3, mime="image/jpeg", type=5, desc="Booklet", data=booklet_art))
    tags.save(mp3_path)

    new_art = _jpeg((100, 150, 200))
    cache_dir = get_settings().image_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    art_sha = hashlib.sha256(new_art).hexdigest()
    cache_path = cache_dir / f"{art_sha}.img"
    cache_path.write_bytes(new_art)

    with session_scope() as db:
        group = AlbumGroup(group_key=token, album="Album", album_artist="Artist",
                           common_dir=f"/{token}/Album", track_count=1)
        db.add(group)
        db.flush()

        track = Track(
            group_id=group.id,
            path="/" + mp3_path.relative_to(root).as_posix(),
            title="First",
            artist="Artist",
            album="Album",
            album_artist="Artist",
        )
        db.add(track)
        db.flush()

        monkeypatch.setattr(service, "refresh_track", lambda selected: None)
        session = create_search_session(db, [track], selection_kind="tracks")
        session_id = session.id

        target = db.scalar(select(SearchTarget).where(SearchTarget.session_id == session_id))
        assert target.artwork_role == QueryRole.album

        asset = ArtworkAsset(
            sha256=art_sha, phash="0000000000000000", mime_type="image/jpeg",
            width=300, height=300, cache_path=str(cache_path),
        )
        db.add(asset)
        db.flush()

        cluster = ArtworkCluster(
            query_id=target.album_query_id, representative_asset_id=asset.id,
            score=0.9, confidence_label="high",
        )
        db.add(cluster)
        db.flush()

        target.selected_album_cluster_id = cluster.id
        target.album_approved = True

    # Apply search session
    result = apply_search_session(session_id)
    assert "applied 1 tracks; 0 failed" in result

    # Check file on disk
    reloaded = ID3(mp3_path)
    pics = reloaded.getall("APIC")
    assert len([p for p in pics if p.type == 3]) == 1
    assert not any(p.type == 0 and p.desc == TRACK_DESCRIPTION for p in pics)
    assert any(p.type == 5 and bytes(p.data) == booklet_art for p in pics)
    assert reloaded.version == (2, 3, 0)

    # Check audit record
    with session_scope() as db:
        target = db.scalar(select(SearchTarget).where(SearchTarget.session_id == session_id))
        assert target.applied is True
        assert target.status == WorkStatus.applied

        audit = db.scalar(select(TrackWriteAudit).where(TrackWriteAudit.target_id == target.id))
        assert audit is not None
        assert audit.roles == "album"
        assert audit.ok is True
        assert audit.wrote_cover_jpg is True  # whole directory agreed
