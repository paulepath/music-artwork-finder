from __future__ import annotations

import hashlib
import io
import struct
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, TIT2, TPE1, TPE2
from mutagen.mp4 import MP4
from PIL import Image
from sqlalchemy import select

from app.config import get_settings
from app.db import init_db, session_scope
from app.dual_writer import apply_track_artwork, fingerprint
from app.main import app
from app.models import (
    AlbumGroup,
    ArtworkAsset,
    ArtworkCluster,
    ArtworkQuery,
    GroupState,
    QueryRole,
    SearchSession,
    SearchStatus,
    SearchTarget,
    Track,
    TrackWriteAudit,
    WorkStatus,
)
from app.search_service import apply_search_session, create_search_session
from app.writer import restore_files


def _jpeg(colour: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (100, 100), colour).save(out, "JPEG")
    return out.getvalue()


def _tagged_mp3(path: Path, *, title: str = "", artist: str = "", album: str = "", albumartist: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    tags = ID3()
    if title:
        tags.add(TIT2(encoding=3, text=title))
    if artist:
        tags.add(TPE1(encoding=3, text=artist))
    if albumartist:
        tags.add(TPE2(encoding=3, text=albumartist))
    if album:
        tags.add(TALB(encoding=3, text=album))
    tags.save(path)


def _make_flac(path: Path, *, title: str = "", artist: str = "", album: str = "", albumartist: str = ""):
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
    audio = FLAC(str(path))
    if title:
        audio["title"] = title
    if artist:
        audio["artist"] = artist
    if albumartist:
        audio["albumartist"] = albumartist
    if album:
        audio["album"] = album
    audio.save()


def _make_m4a(path: Path, *, title: str = "", artist: str = "", album: str = "", albumartist: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    ftyp_data = b"M4A \x00\x00\x02\x00M4A mp42isom"
    ftyp = struct.pack(">I", len(ftyp_data) + 8) + b"ftyp" + ftyp_data
    moov = struct.pack(">I", 8) + b"moov"
    path.write_bytes(ftyp + moov)
    raw_mp4 = MP4(str(path))
    raw_mp4.add_tags()
    raw_mp4.save()
    audio = EasyMP4(str(path))
    if title:
        audio["title"] = [title]
    if artist:
        audio["artist"] = [artist]
    if albumartist:
        audio["albumartist"] = [albumartist]
    if album:
        audio["album"] = [album]
    audio.save()


def test_tag_write_mp3_flac_m4a():
    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root / token

    mp3_path = root / "test.mp3"
    flac_path = root / "test.flac"
    m4a_path = root / "test.m4a"

    _tagged_mp3(mp3_path, title="Song 1", artist="Old Artist", album="Old Album")
    _make_flac(flac_path, title="Song 2", artist="Old Artist", album="Old Album")
    _make_m4a(m4a_path, title="Song 3", artist="Old Artist", album="Old Album")

    with session_scope() as db:
        group = AlbumGroup(group_key=token, album="Old Album", album_artist="Old Artist", common_dir=f"/{token}", track_count=3)
        db.add(group)
        db.flush()
        t1 = Track(group_id=group.id, path=f"/{token}/test.mp3", title="Song 1", artist="Old Artist", album="Old Album", album_artist="Old Artist", file_format="mp3")
        t2 = Track(group_id=group.id, path=f"/{token}/test.flac", title="Song 2", artist="Old Artist", album="Old Album", album_artist="Old Artist", file_format="flac")
        t3 = Track(group_id=group.id, path=f"/{token}/test.m4a", title="Song 3", artist="Old Artist", album="Old Album", album_artist="Old Artist", file_format="m4a")
        db.add_all([t1, t2, t3])
        db.flush()

        tags = {"album": "New Album", "artist": "New Artist", "albumartist": "New Album Artist"}
        for t in [t1, t2, t3]:
            backup, _, _ = apply_track_artwork(t, tags=tags)
            assert backup

    mp3_tags = EasyID3(str(mp3_path))
    assert mp3_tags["album"] == ["New Album"]
    assert mp3_tags["artist"] == ["New Artist"]
    assert mp3_tags["albumartist"] == ["New Album Artist"]

    flac_tags = FLAC(str(flac_path))
    assert flac_tags["album"] == ["New Album"]
    assert flac_tags["artist"] == ["New Artist"]
    assert flac_tags["albumartist"] == ["New Album Artist"]

    m4a_tags = EasyMP4(str(m4a_path))
    assert m4a_tags["album"] == ["New Album"]
    assert m4a_tags["artist"] == ["New Artist"]
    assert m4a_tags["albumartist"] == ["New Album Artist"]


def test_tag_write_undo_restores_original_tags():
    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root / token

    mp3_path = root / "undo_test.mp3"
    _tagged_mp3(mp3_path, title="Song", artist="Original Artist", album="Original Album")

    with session_scope() as db:
        group = AlbumGroup(group_key=token, album="Original Album", album_artist="Original Artist", common_dir=f"/{token}", track_count=1)
        db.add(group)
        db.flush()
        track = Track(group_id=group.id, path=f"/{token}/undo_test.mp3", title="Song", artist="Original Artist", album="Original Album", album_artist="Original Artist", file_format="mp3")
        db.add(track)
        db.flush()

        backup, _, _ = apply_track_artwork(track, tags={"album": "Modified Album", "artist": "Modified Artist"})

    assert EasyID3(str(mp3_path))["album"] == ["Modified Album"]

    restored = restore_files(backup)
    assert restored == 1

    reloaded = EasyID3(str(mp3_path))
    assert reloaded["album"] == ["Original Album"]
    assert reloaded["artist"] == ["Original Artist"]


def test_identify_endpoint_writes_tags_and_restamps_target_fingerprint_and_undo():
    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root / token
    mp3_path = root / "track1.mp3"
    _tagged_mp3(mp3_path, title="Subwoofer Lullaby", artist="", album="")

    with session_scope() as db:
        group = AlbumGroup(
            group_key=token,
            album="",
            album_artist="",
            common_dir=f"/{token}",
            track_count=1,
            state=GroupState.scanned,
        )
        db.add(group)
        db.flush()
        stat = mp3_path.stat()
        track = Track(
            group_id=group.id,
            path=f"/{token}/track1.mp3",
            title="Subwoofer Lullaby",
            artist="",
            album="",
            album_artist="",
            file_format="mp3",
            file_size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
        db.add(track)
        db.flush()

        session = create_search_session(db, [track], selection_kind="albums")
        session.status = SearchStatus.review_ready
        orig_fp = fingerprint(track)

        group_id = group.id
        track_id = track.id
        session_id = session.id
        target = db.scalar(select(SearchTarget).where(SearchTarget.session_id == session.id))
        target_id = target.id

    client = TestClient(app)

    # Call identify endpoint with write_tags=True
    resp = client.post(f"/api/albums/{group_id}/identify", json={
        "album": "Minecraft - Volume Alpha",
        "artist": "C418",
        "mbid": "mbid-123",
        "release_group_id": "rg-456",
        "write_tags": True,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["tags_written"] == 1
    assert data["identified_album"] == "Minecraft - Volume Alpha"
    assert data["identified_artist"] == "C418"

    # Verify audio tags written to disk
    tags = EasyID3(str(mp3_path))
    assert tags["album"] == ["Minecraft - Volume Alpha"]
    assert tags["artist"] == ["C418"]
    assert tags["albumartist"] == ["C418"]

    # Verify track and target fingerprint updated in DB
    with session_scope() as db:
        t = db.get(Track, track_id)
        assert t.album == "Minecraft - Volume Alpha"
        assert t.artist == "C418"
        assert t.album_artist == "C418"
        new_fp = fingerprint(t)
        assert new_fp != orig_fp

        tgt = db.get(SearchTarget, target_id)
        assert tgt.fingerprint == new_fp, "Target fingerprint must be re-stamped so apply does not reject it"

        # Verify TrackWriteAudit row
        audit_row = db.scalar(
            select(TrackWriteAudit)
            .where(TrackWriteAudit.track_id == track_id, TrackWriteAudit.action == "tags")
        )
        assert audit_row is not None
        assert audit_row.ok is True
        audit_id = audit_row.id

    # Test undo of the tag write via /api/track-audit/{audit_id}/undo
    undo_resp = client.post(f"/api/track-audit/{audit_id}/undo")
    assert undo_resp.status_code == 200, undo_resp.text
    assert undo_resp.json()["restored"] == 1

    # Verify restored tags on disk
    reloaded = EasyID3(str(mp3_path))
    assert "album" not in reloaded or reloaded["album"] == [""]

    # Verify DB track fields and target fingerprint re-stamped back
    with session_scope() as db:
        t = db.get(Track, track_id)
        assert t.album == ""
        tgt = db.get(SearchTarget, target_id)
        assert tgt.fingerprint == fingerprint(t)


def test_apply_search_session_succeeds_after_tags_written():
    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root / token
    mp3_path = root / "track.mp3"
    _tagged_mp3(mp3_path, title="Song", artist="", album="")

    with session_scope() as db:
        group = AlbumGroup(
            group_key=token,
            album="",
            album_artist="",
            common_dir=f"/{token}",
            track_count=1,
            state=GroupState.scanned,
        )
        db.add(group)
        db.flush()
        stat = mp3_path.stat()
        track = Track(
            group_id=group.id,
            path=f"/{token}/track.mp3",
            title="Song",
            artist="",
            album="",
            album_artist="",
            file_format="mp3",
            file_size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
        db.add(track)
        db.flush()

        session = create_search_session(db, [track], selection_kind="albums")
        session_id = session.id
        group_id = group.id

        # Setup mock cluster and asset for album query
        query = db.scalar(select(ArtworkQuery).where(ArtworkQuery.session_id == session.id, ArtworkQuery.role == QueryRole.album))
        art_bytes = _jpeg((12, 34, 56))
        art_sha = hashlib.sha256(f"asset-{token}".encode()).hexdigest()
        cache = get_settings().image_cache_dir / f"{art_sha}.img"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(art_bytes)

        asset = ArtworkAsset(
            sha256=art_sha,
            phash="0000000000000000",
            mime_type="image/jpeg",
            width=100,
            height=100,
            cache_path=str(cache),
        )
        db.add(asset)
        db.flush()
        cluster = ArtworkCluster(query_id=query.id, representative_asset_id=asset.id)
        db.add(cluster)
        db.flush()

        target = db.scalar(select(SearchTarget).where(SearchTarget.session_id == session.id))
        target.artwork_role = QueryRole.album
        target.selected_album_cluster_id = cluster.id
        target.album_approved = True
        session.status = SearchStatus.review_ready

    client = TestClient(app)

    # Write tags via identify endpoint
    resp = client.post(f"/api/albums/{group_id}/identify", json={
        "album": "Album Identified",
        "artist": "Artist Identified",
        "write_tags": True,
    })
    assert resp.status_code == 200
    assert resp.json()["tags_written"] == 1

    # Now apply search session; because target fingerprint was re-stamped, apply must succeed!
    result = apply_search_session(session_id)
    assert "applied 1 tracks; 0 failed" in result

    with session_scope() as db:
        tgt = db.scalar(select(SearchTarget).where(SearchTarget.session_id == session_id))
        assert tgt.applied is True
        assert tgt.status == WorkStatus.applied


def test_track_write_audits_schema_migration_handles_legacy_notnull():
    from sqlalchemy import text
    from app.db import engine, init_db, session_scope

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS track_write_audits"))
        conn.execute(
            text(
                """
                CREATE TABLE track_write_audits (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    track_id INTEGER NOT NULL,
                    action VARCHAR(32) NOT NULL,
                    roles VARCHAR(64) NOT NULL,
                    backup_dir TEXT NOT NULL,
                    image_sha256 TEXT NOT NULL,
                    wrote_cover_jpg BOOLEAN NOT NULL,
                    ok BOOLEAN NOT NULL,
                    error TEXT NOT NULL,
                    created_at DATETIME NOT NULL,
                    undone_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO track_write_audits (
                    session_id, target_id, track_id, action, roles,
                    backup_dir, image_sha256, wrote_cover_jpg, ok, error,
                    created_at
                ) VALUES (
                    1, 2, 3, 'apply', 'album', '/tmp/backup', 'sha', 1, 1, '', '2026-01-01 00:00:00'
                )
                """
            )
        )

    init_db()

    with session_scope() as db:
        db.add(
            TrackWriteAudit(
                session_id=None,
                target_id=None,
                track_id=3,
                action="tags",
                roles="tags",
                backup_dir="/tmp/b2",
                ok=True,
            )
        )
        db.commit()

        audits = list(db.scalars(select(TrackWriteAudit)))
        assert len(audits) >= 2
