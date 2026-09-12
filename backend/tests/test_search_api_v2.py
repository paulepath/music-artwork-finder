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

