from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.db import init_db, session_scope
from app.identify import guess_album_title, identify_album, score_release
from app.main import app
from app.models import AlbumGroup, GroupState, QueryRole, SearchTarget, Track
from app.schemas import IdentifyApplyRequest
from app.search_service import create_search_session
from tests.test_search_v2 import _tagged_mp3


def test_guess_album_title():
    assert guess_album_title("/Minecraft_OST") == "Minecraft OST"
    assert guess_album_title("/Music/(2011) Terrace Mix") == "Terrace Mix"
    assert guess_album_title("/Music/[2004] Album_Name") == "Album Name"
    assert guess_album_title("/Music/Box Set/CD1") == "Box Set"
    assert guess_album_title("/Music/Box Set/Disc 2") == "Box Set"
    assert guess_album_title("") == ""


def test_set_based_tracklist_scoring_handles_none_track_numbers():
    local_titles = ["Subwoofer Lullaby", "Wet Hands", "Dry Hands", "Minecraft"]
    # All track_no are None in local track list

    matching_mb_release = {
        "id": "mbid-minecraft-123",
        "title": "Minecraft - Volume Alpha",
        "artist-credit": [{"name": "C418", "joinphrase": ""}],
        "date": "2011-03-04",
        "release-group": {"id": "rg-123"},
        "media": [
            {
                "tracks": [
                    {"title": "Key", "length": 65000},
                    {"title": "Door", "length": 111000},
                    {"title": "Subwoofer Lullaby", "length": 208000},
                    {"title": "Death", "length": 41000},
                    {"title": "Living Mice", "length": 177000},
                    {"title": "Moog City", "length": 160000},
                    {"title": "Haggstrom", "length": 204000},
                    {"title": "Minecraft", "length": 254000},
                    {"title": "Wet Hands", "length": 90000},
                    {"title": "Dry Hands", "length": 68000},
                ]
            }
        ],
    }

    unrelated_mb_release = {
        "id": "mbid-unrelated-999",
        "title": "Greatest Hits of the 80s",
        "artist-credit": [{"name": "Various", "joinphrase": ""}],
        "date": "1989-01-01",
        "release-group": {"id": "rg-999"},
        "media": [
            {
                "tracks": [
                    {"title": "Take On Me", "length": 220000},
                    {"title": "Billie Jean", "length": 290000},
                ]
            }
        ],
    }

    scored_match = score_release(
        matching_mb_release,
        guess="Minecraft OST",
        local_titles=local_titles,
        track_count=4,
    )
    assert scored_match["matched_titles"] == 4
    assert scored_match["score"] > 0.7
    assert scored_match["artist"] == "C418"
    assert scored_match["year"] == 2011

    scored_unrelated = score_release(
        unrelated_mb_release,
        guess="Minecraft OST",
        local_titles=local_titles,
        track_count=4,
    )
    assert scored_unrelated["matched_titles"] == 0
    assert scored_unrelated["score"] < 0.2


@pytest.mark.asyncio
async def test_recording_seeded_path_finds_release_and_caps_calls(monkeypatch):
    # Test that Seed B caps calls to at most 5 sampled recordings and at most 8 releases
    import app.identify as identify_module

    call_counts = {"recording": 0, "release_search": 0, "release_lookup": 0}

    async def mock_get_json(client, url, **kw):
        params = kw.get("params", {})
        if "/release?" in url or url.endswith("/release"):
            call_counts["release_search"] += 1
            # Seed A finds nothing for a useless folder name
            return {"releases": []}
        elif "/recording" in url:
            call_counts["recording"] += 1
            query = params.get("query", "")
            return {
                "recordings": [
                    {
                        "score": 90,
                        "releases": [
                            {"id": f"rel-{call_counts['recording']}-1"},
                            {"id": f"rel-{call_counts['recording']}-2"},
                            {"id": f"rel-{call_counts['recording']}-3"},
                        ],
                    }
                ]
            }
        elif "/release/" in url:
            call_counts["release_lookup"] += 1
            rel_id = url.split("/release/")[1].split("?")[0]
            return {
                "id": rel_id,
                "title": f"Rescued Album {rel_id}",
                "artist-credit": [{"name": "Rescued Artist", "joinphrase": ""}],
                "date": "2020-01-01",
                "release-group": {"id": "rg-1"},
                "media": [{"tracks": [{"title": "Track 1", "length": 180000}]}],
            }
        return None

    monkeypatch.setattr(identify_module, "get_json", mock_get_json)

    # 15 local tracks
    local_titles = [f"Track {i}" for i in range(1, 16)]
    mock_client = AsyncMock()

    candidates = await identify_album(
        mock_client,
        guess="Random Rip",
        local_titles=local_titles,
        track_count=15,
        artist_hint="",
    )

    # Seed B must sample at most 5 tracks, not all 15!
    assert call_counts["recording"] <= 5
    # Total release detail lookups must be capped to at most 8
    assert call_counts["release_lookup"] <= 8
    assert len(candidates) <= 8
    assert len(candidates) > 0


def test_identify_api_flow(monkeypatch):
    import app.identify as identify_module
    import app.search_service as service
    monkeypatch.setattr(service, "refresh_track", lambda selected: None)

    async def mock_get_json(client, url, **kw):
        if url.endswith("/release"):
            return {
                "releases": [
                    {
                        "id": "mbid-mc-alpha",
                        "title": "Minecraft - Volume Alpha",
                    }
                ]
            }
        elif "/release/mbid-mc-alpha" in url:
            return {
                "id": "mbid-mc-alpha",
                "title": "Minecraft - Volume Alpha",
                "artist-credit": [{"name": "C418", "joinphrase": ""}],
                "date": "2011-03-04",
                "release-group": {"id": "rg-mc-alpha"},
                "media": [
                    {
                        "tracks": [
                            {"title": "Subwoofer Lullaby", "length": 208000},
                            {"title": "Living Mice", "length": 177000},
                        ]
                    }
                ],
            }
        return None

    monkeypatch.setattr(identify_module, "get_json", mock_get_json)

    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root
    folder = root / token / "Minecraft_OST"
    t1_path = folder / "Subwoofer Lullaby.mp3"
    t2_path = folder / "Living Mice.mp3"

    _tagged_mp3(t1_path, title="Subwoofer Lullaby", artist="", album="")
    _tagged_mp3(t2_path, title="Living Mice", artist="", album="")

    with session_scope() as db:
        g = AlbumGroup(
            group_key=f"{token}-mc", album="", album_artist="",
            common_dir=f"/{token}/Minecraft_OST", track_count=2, state=GroupState.needs_tagging
        )
        db.add(g)
        db.flush()

        tr1 = Track(group_id=g.id, path="/" + t1_path.relative_to(root).as_posix(),
                    title="Subwoofer Lullaby", artist="", album="", album_artist="", disc=1)
        tr2 = Track(group_id=g.id, path="/" + t2_path.relative_to(root).as_posix(),
                    title="Living Mice", artist="", album="", album_artist="", disc=1)
        db.add_all([tr1, tr2])
        db.commit()
        group_id = g.id
        track_ids = [tr1.id, tr2.id]

    client = TestClient(app)

    # 1. GET /api/albums/{id}/identify
    res = client.get(f"/api/albums/{group_id}/identify")
    assert res.status_code == 200
    candidates = res.json()
    assert len(candidates) >= 1
    best = candidates[0]
    assert best["mbid"] == "mbid-mc-alpha"
    assert best["artist"] == "C418"
    assert best["matched_titles"] == 2

    # 2. POST /api/albums/{id}/identify
    post_res = client.post(
        f"/api/albums/{group_id}/identify",
        json={
            "mbid": best["mbid"],
            "release_group_id": best["release_group_id"],
            "album": best["title"],
            "artist": best["artist"],
        },
    )
    assert post_res.status_code == 200
    assert post_res.json()["ok"] is True
    assert post_res.json()["identified_album"] == "Minecraft - Volume Alpha"

    # 3. GET /api/albums/{id} returns identified fields
    detail_res = client.get(f"/api/albums/{group_id}")
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert detail["identified_album"] == "Minecraft - Volume Alpha"
    assert detail["identified_artist"] == "C418"
    assert detail["identified_mbid"] == "mbid-mc-alpha"
    assert detail["identified_release_group_id"] == "rg-mc-alpha"

    # 4. create_search_session wires identified_album so governing becomes album
    with session_scope() as db:
        tracks = list(db.scalars(select(Track).where(Track.id.in_(track_ids))))
        session = create_search_session(db, tracks, selection_kind="albums")
        targets = list(db.scalars(select(SearchTarget).where(SearchTarget.session_id == session.id)))
        assert len(targets) == 2
        # Both targets now governed by album role!
        for target in targets:
            assert target.artwork_role == QueryRole.album
            assert target.search_album == "Minecraft - Volume Alpha"
            assert target.search_album_artist == "C418"
