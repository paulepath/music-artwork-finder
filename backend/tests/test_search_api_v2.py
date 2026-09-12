from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_library_and_search_session_endpoints_start_cleanly():
    with TestClient(app) as client:
        health = client.get("/api/health")
        folders = client.get("/api/library/folders", params={"parent": "/"})
        sessions = client.get("/api/search-sessions")

    assert health.status_code == 200
    assert folders.status_code == 200
    assert folders.json()["parent"] == "/"
    assert sessions.status_code == 200


def test_folder_browser_rejects_parent_traversal():
    with TestClient(app) as client:
        response = client.get("/api/library/folders", params={"parent": "/../etc"})

    assert response.status_code == 400


def test_empty_track_selection_is_rejected():
    with TestClient(app) as client:
        response = client.post("/api/search-sessions/preview", json={"track_ids": []})

    assert response.status_code == 422


def test_search_selection_by_album_ids():
    import uuid
    from app.config import get_settings
    from app.db import init_db, session_scope
    from app.models import AlbumGroup, AlbumMerge, SearchSession, Track

    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root

    with session_scope() as db:
        merge = AlbumMerge(merge_key=f"m-{token}", title="Merged Set", album_artist="Artist")
        db.add(merge)
        db.flush()

        g1 = AlbumGroup(
            group_key=f"g1-{token}", album="Set (Disc 1)", album_artist="Artist",
            disc=1, common_dir=f"/{token}/Set/CD1", merged_into_id=merge.id,
        )
        g2 = AlbumGroup(
            group_key=f"g2-{token}", album="Set (Disc 2)", album_artist="Artist",
            disc=2, common_dir=f"/{token}/Set/CD2", merged_into_id=merge.id,
        )
        db.add_all([g1, g2])
        db.flush()
        g1_id, g2_id = g1.id, g2.id

        # Lay down real files so path.stat() succeeds on refresh
        for i in range(1, 3):
            p1 = root / token / "Set" / "CD1" / f"{i:02d}.mp3"
            p1.parent.mkdir(parents=True, exist_ok=True)
            p1.touch()
            t1 = Track(group_id=g1_id, path="/" + p1.relative_to(root).as_posix(), title=f"D1 T{i}", album="Set", artist="Artist")
            p2 = root / token / "Set" / "CD2" / f"{i:02d}.mp3"
            p2.parent.mkdir(parents=True, exist_ok=True)
            p2.touch()
            t2 = Track(group_id=g2_id, path="/" + p2.relative_to(root).as_posix(), title=f"D2 T{i}", album="Set", artist="Artist")
            db.add_all([t1, t2])
        db.flush()

    with TestClient(app) as client:
        # Unknown album ID -> 404
        res404 = client.post("/api/search-sessions/preview", json={"album_ids": [999999]})
        assert res404.status_code == 404

        # Conflicting sources -> 422
        res422 = client.post("/api/search-sessions/preview", json={"album_ids": [g1_id], "track_ids": [1]})
        assert res422.status_code == 422

        # Preview expands single member of merged set to all 4 tracks of both discs
        res_preview = client.post("/api/search-sessions/preview", json={"album_ids": [g1_id]})
        assert res_preview.status_code == 200
        data = res_preview.json()
        assert data["selection_kind"] == "albums"
        assert data["track_count"] == 4

        # Start search creates session with kind="albums"
        res_start = client.post("/api/search-sessions", json={"album_ids": [g1_id]})
        assert res_start.status_code == 200
        sid = res_start.json()["session_id"]

        with session_scope() as db:
            session = db.get(SearchSession, sid)
            assert session is not None
            assert session.selection_kind == "albums"
            assert session.total_tracks == 4

        # The review page polls once a second and reads albums from /albums, so it must be able
        # to ask for the header without the per-target payload (which repeats an album's
        # clusters once per member track).
        full = client.get(f"/api/search-sessions/{sid}")
        light = client.get(f"/api/search-sessions/{sid}", params={"include_targets": "false"})
        assert full.status_code == light.status_code == 200
        assert len(full.json()["targets"]) == 4
        assert "targets" not in light.json()
        assert light.json()["total_tracks"] == 4


