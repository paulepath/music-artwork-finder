from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.db import init_db, session_scope
from app.main import app
from app.models import (
    AlbumGroup,
    AlbumMerge,
    GroupState,
    Job,
    JobKind,
    QueryRole,
    SearchSession,
    SearchStatus,
    SearchTarget,
    Track,
)
from app.search_service import create_search_session
from tests.test_search_v2 import _tagged_mp3


@pytest.fixture
def artwork_view_client(monkeypatch):
    import app.search_service as service
    monkeypatch.setattr(service, "refresh_track", lambda selected: None)

    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root
    album_dir = root / token / "Artist" / "Album1"
    disc1_dir = root / token / "Artist" / "MultiAlbum" / "CD1"
    disc2_dir = root / token / "Artist" / "MultiAlbum" / "CD2"
    parent_dir = root / token / "Various"

    t1_path = album_dir / "01.mp3"
    t2_path = album_dir / "02.mp3"
    t3_path = disc1_dir / "01.mp3"
    t4_path = disc2_dir / "01.mp3"
    t5_path = parent_dir / "loose.mp3"

    _tagged_mp3(t1_path, title="Song 1", artist="Artist", album="Album 1")
    _tagged_mp3(t2_path, title="Song 2", artist="Artist", album="Album 1")
    _tagged_mp3(t3_path, title="Disc 1 Song", artist="Artist", album="Multi Disc")
    _tagged_mp3(t4_path, title="Disc 2 Song", artist="Artist", album="Multi Disc")
    _tagged_mp3(t5_path, title="Loose", artist="Various", album="")

    with session_scope() as db:
        g1 = AlbumGroup(
            group_key=f"{token}-1", album="Album 1", album_artist="Artist",
            common_dir=f"/{token}/Artist/Album1", track_count=2, state=GroupState.scanned
        )
        g_multi_parent = AlbumGroup(
            group_key=f"{token}-parent", album="", album_artist="Various",
            common_dir=f"/{token}/Various", track_count=1, state=GroupState.multi_album_parent
        )
        g_cd1 = AlbumGroup(
            group_key=f"{token}-cd1", album="Multi Disc", album_artist="Artist", disc=1,
            common_dir=f"/{token}/Artist/MultiAlbum/CD1", track_count=1, state=GroupState.scanned
        )
        g_cd2 = AlbumGroup(
            group_key=f"{token}-cd2", album="Multi Disc", album_artist="Artist", disc=2,
            common_dir=f"/{token}/Artist/MultiAlbum/CD2", track_count=1, state=GroupState.scanned
        )
        db.add_all([g1, g_multi_parent, g_cd1, g_cd2])
        db.flush()

        merge = AlbumMerge(
            merge_key=f"artist|||multi disc|||/{token}/Artist/MultiAlbum",
            title="Multi Disc",
            album_artist="Artist"
        )
        db.add(merge)
        db.flush()
        g_cd1.merged_into_id = merge.id
        g_cd2.merged_into_id = merge.id

        tr1 = Track(group_id=g1.id, path="/" + t1_path.relative_to(root).as_posix(),
                    title="Song 1", artist="Artist", album="Album 1", album_artist="Artist", disc=1, track_no=1)
        tr2 = Track(group_id=g1.id, path="/" + t2_path.relative_to(root).as_posix(),
                    title="Song 2", artist="Artist", album="Album 1", album_artist="Artist", disc=1, track_no=2)
        tr3 = Track(group_id=g_cd1.id, path="/" + t3_path.relative_to(root).as_posix(),
                    title="Disc 1 Song", artist="Artist", album="Multi Disc", album_artist="Artist", disc=1, track_no=1)
        tr4 = Track(group_id=g_cd2.id, path="/" + t4_path.relative_to(root).as_posix(),
                    title="Disc 2 Song", artist="Artist", album="Multi Disc", album_artist="Artist", disc=2, track_no=1)
        tr5 = Track(group_id=g_multi_parent.id, path="/" + t5_path.relative_to(root).as_posix(),
                    title="Loose", artist="Various", album="", album_artist="Various", disc=1, track_no=1)
        db.add_all([tr1, tr2, tr3, tr4, tr5])
        db.commit()

        context = {
            "g1_id": g1.id,
            "g_multi_parent_id": g_multi_parent.id,
            "g_cd1_id": g_cd1.id,
            "g_cd2_id": g_cd2.id,
            "tr1_id": tr1.id,
            "tr2_id": tr2.id,
        }

    return TestClient(app), context


def test_artwork_session_creates_session(artwork_view_client):
    client, ctx = artwork_view_client
    g1_id = ctx["g1_id"]

    res = client.post(f"/api/albums/{g1_id}/artwork-session")
    assert res.status_code == 200
    data = res.json()
    assert "session_id" in data
    assert "album_query_id" in data
    assert data["session_id"] is not None
    assert data["album_query_id"] is not None

    with session_scope() as db:
        session = db.get(SearchSession, data["session_id"])
        assert session is not None
        assert session.selection_kind == "albums"
        assert session.total_tracks == 2

        # Check job created
        jobs = list(db.scalars(select(Job).where(Job.kind == JobKind.search_session)))
        assert any(json.loads(j.payload).get("session_id") == data["session_id"] for j in jobs)


def test_artwork_session_reuses_review_ready_session(artwork_view_client):
    client, ctx = artwork_view_client
    g1_id = ctx["g1_id"]

    # First call creates the session
    res1 = client.post(f"/api/albums/{g1_id}/artwork-session")
    assert res1.status_code == 200
    session_id = res1.json()["session_id"]

    # Mark it review_ready
    with session_scope() as db:
        session = db.get(SearchSession, session_id)
        session.status = SearchStatus.review_ready
        db.commit()

    # Second call should reuse the same session
    res2 = client.post(f"/api/albums/{g1_id}/artwork-session")
    assert res2.status_code == 200
    assert res2.json()["session_id"] == session_id
    assert res2.json()["album_query_id"] == res1.json()["album_query_id"]


def test_artwork_session_refuses_multi_album_parent(artwork_view_client):
    client, ctx = artwork_view_client
    g_multi_parent_id = ctx["g_multi_parent_id"]

    res = client.post(f"/api/albums/{g_multi_parent_id}/artwork-session")
    assert res.status_code == 409
    assert "untitled" in res.json()["detail"].lower() or "identify" in res.json()["detail"].lower()


def test_resolve_tracks_refuses_multi_album_parent(artwork_view_client):
    client, ctx = artwork_view_client
    g_multi_parent_id = ctx["g_multi_parent_id"]

    res = client.post("/api/search-sessions", json={"album_ids": [g_multi_parent_id]})
    assert res.status_code == 422
    assert "untitled" in res.json()["detail"].lower()


def test_artwork_session_spans_merged_groups(artwork_view_client):
    client, ctx = artwork_view_client
    g_cd1_id = ctx["g_cd1_id"]

    res = client.post(f"/api/albums/{g_cd1_id}/artwork-session")
    assert res.status_code == 200
    session_id = res.json()["session_id"]

    with session_scope() as db:
        targets = list(db.scalars(select(SearchTarget).where(SearchTarget.session_id == session_id)))
        # Both CD1 and CD2 tracks included (total 2 tracks)
        assert len(targets) == 2
