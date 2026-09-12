from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.consensus import FetchedImage
from app.db import init_db, session_scope
from app.models import (
    AlbumGroup,
    ArtworkCluster,
    ArtworkQuery,
    CandidateObservation,
    QueryRole,
    SearchSession,
    SearchStatus,
    SearchTarget,
    Track,
    WorkStatus,
)
from app.matching import ReleaseMeta
from app.search_service import create_search_session, run_search_session
from app.sources.base import CandidateData
from tests.test_search_v2 import _jpeg, _tagged_mp3


class _StubAlbumSource:
    def __init__(self, name: str):
        self.name = name

    async def find(self, client, group):
        return [CandidateData(
            source=self.name,
            image_url=f"https://{self.name}.example/album.jpg",
            release=ReleaseMeta(source=self.name, title=group.album,
                                artist=group.album_artist, track_count=group.track_count),
        )]


class _NoGoogle:
    def __init__(self, max_results=18):
        pass

    async def find_text(self, client, query, **metadata):
        return []


def _mock_fetched(image_cache_dir):
    async def fetched(client, url, headers=None):
        sha = hashlib.sha256(url.encode()).hexdigest()
        cache = image_cache_dir / f"{sha}.img"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(_jpeg((100, 100, 100)))
        return FetchedImage(True, sha256=sha, phash="0000000000000000",
                            width=300, height=300, cache_path=str(cache))
    return fetched


async def _mock_track_candidates(client, track):
    return [CandidateData(
        source="itunes",
        image_url=f"https://itunes.example/{track.title}.jpg",
        release=ReleaseMeta(source="itunes", title=track.title, artist=track.artist),
    )]


@pytest.mark.asyncio
async def test_track_with_album_tag_governed_by_album_and_track_query_skipped(monkeypatch):
    import app.search_service as service

    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root
    path = root / token / "Album" / "Song1.mp3"
    _tagged_mp3(path, title="Song 1", artist="Singer", album="Great Album")

    with session_scope() as db:
        group = AlbumGroup(group_key=token, album="Great Album", album_artist="Singer",
                           common_dir=f"/{token}/Album", track_count=1)
        db.add(group)
        db.flush()
        track = Track(
            group_id=group.id,
            path="/" + path.relative_to(root).as_posix(),
            title="Song 1",
            artist="Singer",
            album="Great Album",
            album_artist="Singer",
        )
        db.add(track)
        db.flush()
        monkeypatch.setattr(service, "refresh_track", lambda selected: None)
        session = create_search_session(db, [track], selection_kind="tracks")
        session_id = session.id

        target = db.scalar(select(SearchTarget).where(SearchTarget.session_id == session_id))
        tq = db.get(ArtworkQuery, target.track_query_id)
        aq = db.get(ArtworkQuery, target.album_query_id)

        assert target.artwork_role == QueryRole.album
        assert tq.status == WorkStatus.ready
        assert tq.progress == 1.0
        assert "album artwork governs" in tq.message
        assert aq.status == WorkStatus.queued
        assert aq.progress == 0.0

    monkeypatch.setattr(service, "TRUSTED", (_StubAlbumSource("itunes"),))
    monkeypatch.setattr(service, "find_track_candidates", _mock_track_candidates)
    monkeypatch.setattr(service, "fetch_and_hash", _mock_fetched(get_settings().image_cache_dir))
    monkeypatch.setattr(service, "GoogleImagesSource", _NoGoogle)

    detail = await run_search_session(session_id)

    with session_scope() as db:
        target = db.scalar(select(SearchTarget).where(SearchTarget.session_id == session_id))
        tq = db.get(ArtworkQuery, target.track_query_id)
        aq = db.get(ArtworkQuery, target.album_query_id)

        # Track query has zero observations and zero clusters
        tq_observations = db.scalar(select(func.count()).select_from(CandidateObservation).where(
            CandidateObservation.query_id == tq.id))
        tq_clusters = db.scalar(select(func.count()).select_from(ArtworkCluster).where(
            ArtworkCluster.query_id == tq.id))
        assert tq_observations == 0
        assert tq_clusters == 0

        # Album query was searched and produced clusters
        aq_clusters = db.scalar(select(func.count()).select_from(ArtworkCluster).where(
            ArtworkCluster.query_id == aq.id))
        assert aq_clusters > 0
        assert target.selected_album_cluster_id is not None
        assert target.selected_track_cluster_id is None


@pytest.mark.asyncio
async def test_track_with_empty_album_tag_governed_by_track_and_album_query_skipped(monkeypatch):
    import app.search_service as service

    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root
    path = root / token / "Singles" / "SingleSong.mp3"
    _tagged_mp3(path, title="Single Song", artist="Solo Artist", album="")

    with session_scope() as db:
        group = AlbumGroup(group_key=token, album="", album_artist="Solo Artist",
                           common_dir=f"/{token}/Singles", track_count=1)
        db.add(group)
        db.flush()
        track = Track(
            group_id=group.id,
            path="/" + path.relative_to(root).as_posix(),
            title="Single Song",
            artist="Solo Artist",
            album="",
            album_artist="Solo Artist",
        )
        db.add(track)
        db.flush()
        monkeypatch.setattr(service, "refresh_track", lambda selected: None)
        session = create_search_session(db, [track], selection_kind="tracks")
        session_id = session.id

        target = db.scalar(select(SearchTarget).where(SearchTarget.session_id == session_id))
        tq = db.get(ArtworkQuery, target.track_query_id)
        aq = db.get(ArtworkQuery, target.album_query_id)

        assert target.artwork_role == QueryRole.track
        assert aq.status == WorkStatus.ready
        assert aq.progress == 1.0
        assert "track artwork governs" in aq.message
        assert tq.status == WorkStatus.queued
        assert tq.progress == 0.0

    monkeypatch.setattr(service, "TRUSTED", (_StubAlbumSource("itunes"),))
    monkeypatch.setattr(service, "find_track_candidates", _mock_track_candidates)
    monkeypatch.setattr(service, "fetch_and_hash", _mock_fetched(get_settings().image_cache_dir))
    monkeypatch.setattr(service, "GoogleImagesSource", _NoGoogle)

    detail = await run_search_session(session_id)

    with session_scope() as db:
        target = db.scalar(select(SearchTarget).where(SearchTarget.session_id == session_id))
        tq = db.get(ArtworkQuery, target.track_query_id)
        aq = db.get(ArtworkQuery, target.album_query_id)

        # Album query has zero observations and zero clusters
        aq_observations = db.scalar(select(func.count()).select_from(CandidateObservation).where(
            CandidateObservation.query_id == aq.id))
        aq_clusters = db.scalar(select(func.count()).select_from(ArtworkCluster).where(
            ArtworkCluster.query_id == aq.id))
        assert aq_observations == 0
        assert aq_clusters == 0

        # Track query was searched and produced clusters
        tq_clusters = db.scalar(select(func.count()).select_from(ArtworkCluster).where(
            ArtworkCluster.query_id == tq.id))
        assert tq_clusters > 0
        assert target.selected_track_cluster_id is not None
        assert target.selected_album_cluster_id is None


@pytest.mark.asyncio
async def test_session_searches_query_needed_by_any_target_even_if_shared_with_skipped(monkeypatch):
    """Ensure the memo-dict trap in Task 1.2 is resolved: a query needed by any target is searched."""
    import app.search_service as service

    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root
    dir_path = root / token / "Mixed"
    track1_path = dir_path / "01.mp3"
    track2_path = dir_path / "02.mp3"

    # Track 1 has album tag -> governing is album
    _tagged_mp3(track1_path, title="Album Track", artist="Artist", album="Shared Album")
    # Track 2 has empty album tag -> governing is track
    _tagged_mp3(track2_path, title="Single Track", artist="Artist", album="")

    with session_scope() as db:
        group = AlbumGroup(group_key=token, album="Shared Album", album_artist="Artist",
                           common_dir=f"/{token}/Mixed", track_count=2)
        db.add(group)
        db.flush()

        t1 = Track(
            group_id=group.id,
            path="/" + track1_path.relative_to(root).as_posix(),
            title="Album Track",
            artist="Artist",
            album="Shared Album",
            album_artist="Artist",
        )
        t2 = Track(
            group_id=group.id,
            path="/" + track2_path.relative_to(root).as_posix(),
            title="Single Track",
            artist="Artist",
            album="",
            album_artist="Artist",
        )
        db.add(t1)
        db.add(t2)
        db.flush()

        monkeypatch.setattr(service, "refresh_track", lambda selected: None)
        # Test creation with t2 first, then t1, to test that order does not skip t1's album query
        session = create_search_session(db, [t2, t1], selection_kind="tracks")
        session_id = session.id

        target1 = db.scalar(select(SearchTarget).where(
            SearchTarget.session_id == session_id, SearchTarget.track_id == t1.id))
        target2 = db.scalar(select(SearchTarget).where(
            SearchTarget.session_id == session_id, SearchTarget.track_id == t2.id))

        assert target1.artwork_role == QueryRole.album
        assert target2.artwork_role == QueryRole.track

        # For target 1, album query is needed and queued
        t1_aq = db.get(ArtworkQuery, target1.album_query_id)
        assert t1_aq.status == WorkStatus.queued

        # For target 1, track query is not needed by any target and ready
        t1_tq = db.get(ArtworkQuery, target1.track_query_id)
        assert t1_tq.status == WorkStatus.ready

        # For target 2, track query is needed and queued
        t2_tq = db.get(ArtworkQuery, target2.track_query_id)
        assert t2_tq.status == WorkStatus.queued

    monkeypatch.setattr(service, "TRUSTED", (_StubAlbumSource("itunes"),))
    monkeypatch.setattr(service, "find_track_candidates", _mock_track_candidates)
    monkeypatch.setattr(service, "fetch_and_hash", _mock_fetched(get_settings().image_cache_dir))
    monkeypatch.setattr(service, "GoogleImagesSource", _NoGoogle)

    await run_search_session(session_id)

    with session_scope() as db:
        # t1's album query was searched
        t1_aq_clusters = db.scalar(select(func.count()).select_from(ArtworkCluster).where(
            ArtworkCluster.query_id == t1_aq.id))
        assert t1_aq_clusters > 0

        # t2's track query was searched
        t2_tq_clusters = db.scalar(select(func.count()).select_from(ArtworkCluster).where(
            ArtworkCluster.query_id == t2_tq.id))
        assert t2_tq_clusters > 0

        # t1's track query was not searched
        t1_tq_clusters = db.scalar(select(func.count()).select_from(ArtworkCluster).where(
            ArtworkCluster.query_id == t1_tq.id))
        assert t1_tq_clusters == 0


def _album_session(monkeypatch, track_count: int):
    """Build an unrun session of ``track_count`` album-governed tracks in one folder."""
    import app.search_service as service

    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root
    with session_scope() as db:
        group = AlbumGroup(group_key=token, album="Big Album", album_artist="Artist",
                           common_dir=f"/{token}/Album", track_count=track_count)
        db.add(group)
        db.flush()
        tracks = []
        for index in range(track_count):
            path = root / token / "Album" / f"{index:02d}.mp3"
            _tagged_mp3(path, title=f"Song {index}", artist="Artist", album="Big Album")
            track = Track(group_id=group.id, path="/" + path.relative_to(root).as_posix(),
                          title=f"Song {index}", artist="Artist", album="Big Album",
                          album_artist="Artist")
            db.add(track)
            tracks.append(track)
        db.flush()
        monkeypatch.setattr(service, "refresh_track", lambda selected: None)
        return create_search_session(db, tracks, selection_kind="tracks").id


def test_skipped_queries_do_not_inflate_session_progress(monkeypatch):
    """A skipped query sits at progress 1.0 so its NOT NULL FK stays satisfied.

    Averaging it into the session would report a 6-track album as ~86% complete
    before a single search had run (6 skipped track queries at 1.0 + 1 real album
    query at 0.0), so only governing queries may count.
    """
    from app.search_service import _refresh_session_progress

    session_id = _album_session(monkeypatch, 6)
    _refresh_session_progress(session_id)
    with session_scope() as db:
        session = db.get(SearchSession, session_id)
        assert session.progress == 0.0


def test_failed_governing_query_marks_target_failed(monkeypatch):
    """The skipped query is always ``ready``, so a target must not be judged on it.

    The old ``tq.status == aq.status == failed`` test could never fire once one
    role is skipped, silently presenting a failed search as ready for review.
    """
    from app.search_service import _refresh_session_progress

    session_id = _album_session(monkeypatch, 2)
    with session_scope() as db:
        target = db.scalar(select(SearchTarget).where(SearchTarget.session_id == session_id))
        db.get(ArtworkQuery, target.album_query_id).status = WorkStatus.failed

    _refresh_session_progress(session_id)

    with session_scope() as db:
        session = db.get(SearchSession, session_id)
        for target in db.scalars(select(SearchTarget).where(
                SearchTarget.session_id == session_id)):
            assert target.status == WorkStatus.failed
        assert session.failed_tracks == 2
