from __future__ import annotations

import io
import hashlib
import uuid

import pytest
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TPE2
from PIL import Image
from sqlalchemy import func, select

from app.consensus import FetchedImage, cluster_phashes
from app.db import init_db, session_scope
from app.dual_writer import TRACK_DESCRIPTION, _write_roles, read_artwork
from app.matching import TrackMeta, classify_track
from app.models import AlbumGroup, ArtworkCluster, ArtworkQuery, QueryRole, SearchSession, SearchStatus, SearchTarget, Track
from app.search_service import create_search_session, run_search_session
from app.sources.base import CandidateData
from app.matching import ReleaseMeta


def _jpeg(colour: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (300, 300), colour).save(out, "JPEG")
    return out.getvalue()


def test_representative_clustering_does_not_chain_near_duplicates():
    a = "0000000000000000"
    b = "00000000000000ff"       # eight bits from A
    c = "000000000000ffff"       # eight bits from B, sixteen from A
    clusters = cluster_phashes([a, b, c], threshold=8)
    assert clusters[0] == clusters[1]
    assert clusters[2] != clusters[0]


def test_track_matching_rejects_same_artist_wrong_title():
    local = TrackMeta(title="Video Games", artist="Lana Del Rey")
    result = classify_track(local, title="Born to Die", artist="Lana Del Rey")
    assert result.rejected is True


def test_mp3_dual_artwork_roundtrip_preserves_unrelated_pictures(tmp_path):
    path = tmp_path / "song.mp3"
    path.touch()
    back = _jpeg((0, 0, 255))
    tags = ID3()
    tags.add(APIC(encoding=3, mime="image/jpeg", type=4, desc="Back", data=back))
    tags.save(path)
    album, track_art = _jpeg((255, 0, 0)), _jpeg((0, 255, 0))

    _write_roles(path, album, track_art, replace_album=True, replace_track=True)

    pictures = ID3(path).getall("APIC")
    assert any(pic.type == 3 and bytes(pic.data) == album for pic in pictures)
    assert any(pic.type == 0 and pic.desc == TRACK_DESCRIPTION and bytes(pic.data) == track_art for pic in pictures)
    assert any(pic.type == 4 and bytes(pic.data) == back for pic in pictures)


def _tagged_mp3(path, *, title: str, artist: str, album: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=artist))
    tags.add(TPE2(encoding=3, text="Various Artists"))
    tags.add(TALB(encoding=3, text=album))
    tags.save(path)


def test_search_session_reuses_one_album_query_for_disc_folders():
    from app.config import get_settings

    init_db()
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root
    first_path = root / token / "Compilation" / "Disc 1" / "01.mp3"
    second_path = root / token / "Compilation" / "Disc 2" / "02.mp3"
    _tagged_mp3(first_path, title="First", artist="Artist One", album="Compilation [Disc 1]")
    _tagged_mp3(second_path, title="Second", artist="Artist Two", album="Compilation [Disc 2]")

    with session_scope() as db:
        groups = []
        tracks = []
        for index, path in enumerate((first_path, second_path), 1):
            group = AlbumGroup(group_key=f"{token}-{index}", album="Compilation", album_artist="Various Artists",
                               common_dir=f"/{token}/Compilation/Disc {index}")
            db.add(group)
            db.flush()
            track = Track(group_id=group.id, path="/" + path.relative_to(root).as_posix(), title=f"Track {index}")
            db.add(track)
            db.flush()
            groups.append(group)
            tracks.append(track)
        session = create_search_session(db, tracks, selection_kind="tracks")
        album_queries = db.scalar(select(func.count()).select_from(ArtworkQuery).where(
            ArtworkQuery.session_id == session.id, ArtworkQuery.role == QueryRole.album))
        track_queries = db.scalar(select(func.count()).select_from(ArtworkQuery).where(
            ArtworkQuery.session_id == session.id, ArtworkQuery.role == QueryRole.track))
        targets = db.scalar(select(func.count()).select_from(SearchTarget).where(
            SearchTarget.session_id == session.id))

    assert album_queries == 1
    assert track_queries == 2
    assert targets == 2


class _StubAlbumSource:
    def __init__(self, name: str, calls: list | None = None):
        self.name = name
        self.calls = calls

    async def find(self, client, group):
        if self.calls is not None:
            self.calls.append((self.name, group.album, group.album_artist, group.track_count))
        return [CandidateData(
            source=self.name, image_url=f"https://{self.name}.example/cover.jpg",
            release=ReleaseMeta(source=self.name, title=group.album,
                                artist=group.album_artist, track_count=group.track_count),
        )]


@pytest.mark.asyncio
async def test_search_pipeline_persists_clusters_progress_and_preselection(monkeypatch):
    import app.search_service as service
    from app.config import get_settings

    init_db()
    album_calls, fetched_urls = [], []
    token = uuid.uuid4().hex[:8]
    root = get_settings().music_root
    path = root / token / "Album" / "Song.mp3"
    _tagged_mp3(path, title="Song", artist="Singer", album="Album")
    with session_scope() as db:
        group = AlbumGroup(group_key=token, album="Album", album_artist="Various Artists",
                           common_dir=f"/{token}/Album", track_count=1)
        db.add(group)
        db.flush()
        track = Track(
            group_id=group.id,
            path="/" + path.relative_to(root).as_posix(),
            title="Song",
            artist="Singer",
            album="Album",
            album_artist="Various Artists",
        )
        db.add(track)
        db.flush()
        monkeypatch.setattr(service, "refresh_track", lambda selected: None)
        session_id = create_search_session(db, [track], selection_kind="tracks").id

    async def track_candidates(client, track):
        return [CandidateData(
            source=name, image_url=f"https://{name}.example/single.jpg",
            release=ReleaseMeta(source=name, title=track.title, artist=track.artist),
        ) for name in ("itunes", "deezer")]

    async def fetched(client, url, headers=None):
        fetched_urls.append(url)
        sha = hashlib.sha256(url.encode()).hexdigest()
        cache = get_settings().image_cache_dir / f"{sha}.img"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(_jpeg((40, 50, 60)))
        return FetchedImage(True, sha256=sha, phash="0000000000000000",
                            width=600, height=600, cache_path=str(cache))

    class NoGoogle:
        def __init__(self, max_results=18):
            pass

        async def find_text(self, client, query, **metadata):
            return []

    monkeypatch.setattr(service, "TRUSTED", (_StubAlbumSource("itunes", album_calls), _StubAlbumSource("deezer", album_calls)))
    monkeypatch.setattr(service, "find_track_candidates", track_candidates)
    monkeypatch.setattr(service, "fetch_and_hash", fetched)
    monkeypatch.setattr(service, "GoogleImagesSource", NoGoogle)

    detail = await run_search_session(session_id)

    with session_scope() as db:
        session = db.get(SearchSession, session_id)
        target = db.scalar(select(SearchTarget).where(SearchTarget.session_id == session_id))
        clusters = db.scalar(select(func.count()).select_from(ArtworkCluster).join(
            ArtworkQuery, ArtworkCluster.query_id == ArtworkQuery.id).where(
                ArtworkQuery.session_id == session_id))
        query_state = [(query.role.value, query.status.value, query.message) for query in db.scalars(
            select(ArtworkQuery).where(ArtworkQuery.session_id == session_id))]
        query_state.append(("selected", target.selected_album_cluster_id, target.selected_track_cluster_id))
        query_state.append(("calls", album_calls, fetched_urls))
    assert "tracks ready" in detail
    assert session.status == SearchStatus.review_ready
    assert session.progress == 1.0, query_state
    assert clusters == 1, query_state
    assert target.selected_album_cluster_id is not None
    assert target.selected_track_cluster_id is None
    assert target.artwork_role == QueryRole.album
