from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.db import init_db, session_scope
from app.main import app
from app.models import (
    AlbumGroup,
    ArtworkAsset,
    ArtworkCluster,
    ArtworkQuery,
    CandidateObservation,
    QueryRole,
    SearchSession,
    SearchTarget,
    Track,
    WorkStatus,
)
from app.search_service import create_search_session
from tests.test_search_v2 import _jpeg, _tagged_mp3


def _setup_session_fixture(monkeypatch):
    import app.search_service as service
    monkeypatch.setattr(service, "refresh_track", lambda selected: None)

    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root
    album_dir = root / token / "Band" / "Album1"
    singles_dir = root / token / "Singles"

    t1_path = album_dir / "01.mp3"
    t2_path = album_dir / "02.mp3"
    t3_path = singles_dir / "single.mp3"

    _tagged_mp3(t1_path, title="Song 1", artist="Band", album="Album 1")
    _tagged_mp3(t2_path, title="Song 2", artist="Band", album="Album 1")
    _tagged_mp3(t3_path, title="Single Track", artist="Solo", album="")

    with session_scope() as db:
        g1 = AlbumGroup(group_key=f"{token}-1", album="Album 1", album_artist="Band",
                        common_dir=f"/{token}/Band/Album1", track_count=2)
        g2 = AlbumGroup(group_key=f"{token}-2", album="", album_artist="Solo",
                        common_dir=f"/{token}/Singles", track_count=1)
        db.add(g1)
        db.add(g2)
        db.flush()

        track1 = Track(group_id=g1.id, path="/" + t1_path.relative_to(root).as_posix(),
                       title="Song 1", artist="Band", album="Album 1", album_artist="Band", disc=1, track_no=1)
        track2 = Track(group_id=g1.id, path="/" + t2_path.relative_to(root).as_posix(),
                       title="Song 2", artist="Band", album="Album 1", album_artist="Band", disc=1, track_no=2)
        track3 = Track(group_id=g2.id, path="/" + t3_path.relative_to(root).as_posix(),
                       title="Single Track", artist="Solo", album="", album_artist="Solo", disc=1, track_no=1)
        db.add_all([track1, track2, track3])
        db.flush()

        session = create_search_session(db, [track1, track2, track3], selection_kind="tracks")
        session_id = session.id

        # Target 1 & 2 share an album_query_id
        target1 = db.scalar(select(SearchTarget).where(
            SearchTarget.session_id == session_id, SearchTarget.track_id == track1.id))
        target3 = db.scalar(select(SearchTarget).where(
            SearchTarget.session_id == session_id, SearchTarget.track_id == track3.id))

        album_query_id = target1.album_query_id
        track_query_id = target3.track_query_id

        # Create assets and clusters
        art_bytes = _jpeg((10, 20, 30))
        art_sha = hashlib.sha256(f"asset-{token}-{uuid.uuid4().hex}".encode()).hexdigest()
        cache = get_settings().image_cache_dir / f"{art_sha}.img"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(art_bytes)

        asset = ArtworkAsset(
            sha256=art_sha, phash="0000000000000000", mime_type="image/jpeg",
            width=300, height=300, cache_path=str(cache),
        )
        db.add(asset)
        db.flush()

        cluster_album = ArtworkCluster(
            query_id=album_query_id, representative_asset_id=asset.id,
            score=0.95, confidence_label="high",
        )
        cluster_track = ArtworkCluster(
            query_id=track_query_id, representative_asset_id=asset.id,
            score=0.4, confidence_label="low",
        )
        db.add(cluster_album)
        db.add(cluster_track)
        db.flush()

        obs = CandidateObservation(
            query_id=album_query_id, cluster_id=cluster_album.id, asset_id=asset.id,
            source="itunes", image_url="https://example.com/cover.jpg",
        )
        db.add(obs)
        db.flush()

        cluster_album_id = cluster_album.id
        cluster_track_id = cluster_track.id

    return {
        "session_id": session_id,
        "album_query_id": album_query_id,
        "track_query_id": track_query_id,
        "cluster_album_id": cluster_album_id,
        "cluster_track_id": cluster_track_id,
        "track1_id": track1.id,
        "track2_id": track2.id,
        "track3_id": track3.id,
    }


def test_get_albums_groups_multi_track_album_and_separates_singles(monkeypatch):
    data = _setup_session_fixture(monkeypatch)
    session_id = data["session_id"]
    album_query_id = data["album_query_id"]

    with TestClient(app) as client:
        resp = client.get(f"/api/search-sessions/{session_id}/albums")
        assert resp.status_code == 200
        payload = resp.json()

        assert "albums" in payload
        assert "singles" in payload

        # Multi-track album grouped into 1 row
        albums = payload["albums"]
        assert len(albums) == 1
        album = albums[0]
        assert album["album_query_id"] == album_query_id
        # Display casing must come from the track tag, not ArtworkQuery.title — the latter holds
        # album_base(), a normalized lookup key, so "Album 1" would render as "album 1".
        assert album["title"] == "Album 1"
        assert album["album_artist"] == "Band"
        assert album["track_count"] == 2
        assert len(album["clusters"]) == 1
        assert len(album["tracks"]) == 2
        assert len(album["directories"]) == 1
        assert album["discs"] == [1]

        # Singles has 1 track-governed target
        singles = payload["singles"]
        assert len(singles) == 1
        single = singles[0]
        assert single["artwork_role"] == "track"
        assert single["artwork_query"]["id"] == data["track_query_id"]
        assert single["track"]["title"] == "Single Track"


def test_put_album_selection_fans_out_to_all_member_targets(monkeypatch):
    data = _setup_session_fixture(monkeypatch)
    session_id = data["session_id"]
    album_query_id = data["album_query_id"]
    cluster_id = data["cluster_album_id"]

    with TestClient(app) as client:
        # PUT album selection
        put_resp = client.put(
            f"/api/search-sessions/{session_id}/albums/{album_query_id}/selection",
            json={"cluster_id": cluster_id, "approved": True},
        )
        assert put_resp.status_code == 200
        assert put_resp.json() == {"ok": True, "targets_updated": 2}

        # Check DB
        with session_scope() as db:
            targets = list(db.scalars(select(SearchTarget).where(
                SearchTarget.session_id == session_id, SearchTarget.album_query_id == album_query_id)))
            assert len(targets) == 2
            for target in targets:
                assert target.selected_album_cluster_id == cluster_id
                assert target.album_approved is True

        # Check GET /albums payload reflects the updated selection
        get_resp = client.get(f"/api/search-sessions/{session_id}/albums")
        assert get_resp.status_code == 200
        album = get_resp.json()["albums"][0]
        assert album["selected_cluster_id"] == cluster_id
        assert album["approved"] is True

        # Clear selection via PUT
        clear_resp = client.put(
            f"/api/search-sessions/{session_id}/albums/{album_query_id}/selection",
            json={"cluster_id": None, "approved": False},
        )
        assert clear_resp.status_code == 200
        assert clear_resp.json() == {"ok": True, "targets_updated": 2}

        with session_scope() as db:
            targets = list(db.scalars(select(SearchTarget).where(
                SearchTarget.session_id == session_id, SearchTarget.album_query_id == album_query_id)))
            for target in targets:
                assert target.selected_album_cluster_id is None
                assert target.album_approved is False


def test_put_album_selection_rejects_cluster_from_different_query(monkeypatch):
    data = _setup_session_fixture(monkeypatch)
    session_id = data["session_id"]
    album_query_id = data["album_query_id"]
    # cluster_track_id belongs to the single's track_query, not album_query
    foreign_cluster_id = data["cluster_track_id"]

    with TestClient(app) as client:
        resp = client.put(
            f"/api/search-sessions/{session_id}/albums/{album_query_id}/selection",
            json={"cluster_id": foreign_cluster_id, "approved": True},
        )
        assert resp.status_code == 422


def test_put_album_selection_rejects_album_query_from_another_session(monkeypatch):
    data1 = _setup_session_fixture(monkeypatch)
    data2 = _setup_session_fixture(monkeypatch)

    with TestClient(app) as client:
        # Album query from session 2 passed to session 1 url
        resp = client.put(
            f"/api/search-sessions/{data1['session_id']}/albums/{data2['album_query_id']}/selection",
            json={"cluster_id": data2["cluster_album_id"], "approved": True},
        )
        assert resp.status_code == 404

        # Non-existent query id
        resp_missing = client.put(
            f"/api/search-sessions/{data1['session_id']}/albums/999999/selection",
            json={"cluster_id": data1["cluster_album_id"], "approved": True},
        )
        assert resp_missing.status_code == 404


def test_approve_recommended_at_high_approves_governing_only_and_leaves_non_governing_untouched(monkeypatch):
    data = _setup_session_fixture(monkeypatch)
    session_id = data["session_id"]
    album_query_id = data["album_query_id"]
    track_query_id = data["track_query_id"]
    high_cluster_id = data["cluster_album_id"]  # confidence_label="high"
    low_cluster_id = data["cluster_track_id"]   # confidence_label="low"

    with session_scope() as db:
        # Preselect high cluster on album targets
        album_targets = list(db.scalars(select(SearchTarget).where(
            SearchTarget.session_id == session_id, SearchTarget.album_query_id == album_query_id)))
        for t in album_targets:
            t.selected_album_cluster_id = high_cluster_id
            # Set non-governing track_approved to True to verify it remains untouched
            t.track_approved = True

        # Preselect low cluster on single target
        single_target = db.scalar(select(SearchTarget).where(
            SearchTarget.session_id == session_id, SearchTarget.track_query_id == track_query_id))
        single_target.selected_track_cluster_id = low_cluster_id
        # Set non-governing album_approved to True to verify it remains untouched
        single_target.album_approved = True

    with TestClient(app) as client:
        resp = client.post(
            f"/api/search-sessions/{session_id}/approve-recommended",
            json={"minimum_confidence": "high"},
        )
        assert resp.status_code == 200
        # Renamed key: approved_targets
        assert resp.json() == {"approved_targets": 2}

        with session_scope() as db:
            for t in db.scalars(select(SearchTarget).where(
                SearchTarget.session_id == session_id, SearchTarget.album_query_id == album_query_id)):
                # Governing album approved
                assert t.album_approved is True
                # Non-governing track_approved was left untouched
                assert t.track_approved is True

            st = db.scalar(select(SearchTarget).where(
                SearchTarget.session_id == session_id, SearchTarget.track_query_id == track_query_id))
            # Governing track was not approved (low < high)
            assert st.track_approved is False
            # Non-governing album_approved was left untouched
            assert st.album_approved is True


def test_target_put_selection_works_with_single_choice(monkeypatch):
    data = _setup_session_fixture(monkeypatch)
    session_id = data["session_id"]
    cluster_id = data["cluster_album_id"]

    with session_scope() as db:
        target = db.scalar(select(SearchTarget).where(
            SearchTarget.session_id == session_id, SearchTarget.artwork_role == QueryRole.album))
        target_id = target.id

    with TestClient(app) as client:
        resp = client.put(
            f"/api/search-sessions/{session_id}/targets/{target_id}/selections",
            json={"cluster_id": cluster_id, "approved": True},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        with session_scope() as db:
            t = db.get(SearchTarget, target_id)
            assert t.selected_album_cluster_id == cluster_id
            assert t.album_approved is True
            # Non-governing untouched
            assert t.selected_track_cluster_id is None
            assert t.track_approved is False
