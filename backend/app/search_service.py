"""Persisted track-first artwork search and dual-artwork application."""
from __future__ import annotations

import asyncio
import posixpath
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import func, select

from .consensus import CLUSTER_THRESHOLD, fetch_and_hash, hamming_distance
from .config import get_settings
from .db import session_scope
from .dual_writer import apply_track_artwork, fingerprint
from .grouping import album_base
from .http import new_client
from .matching import GroupMeta, TrackMeta, classify, classify_track, normalize
from .models import (
    AlbumGroup, ArtworkAsset, ArtworkCluster, ArtworkQuery, CandidateObservation,
    QueryRole, SearchSession, SearchStatus, SearchTarget, Tier, Track,
    TrackWriteAudit, WorkStatus, utcnow,
)
from .sources import DeezerSource, ITunesSource, MusicBrainzSource, PlexSource
from .sources.google_images import GoogleImagesSource
from .sources.tracks import find_track_candidates

TRUSTED = (MusicBrainzSource(), ITunesSource(), DeezerSource())
PLEX = PlexSource()
_GOOGLE_SEMAPHORE = asyncio.Semaphore(1)
_SOURCE_WEIGHT = {"musicbrainz": 4.0, "itunes": 3.0, "deezer": 3.0, "plex": 4.0, "google": 1.0}
_TERMINAL_QUERY = (WorkStatus.ready, WorkStatus.failed, WorkStatus.cancelled)


class SearchCancelled(RuntimeError):
    pass


def refresh_track(track: Track) -> None:
    """Refresh searchable metadata and stat information from the selected file."""
    from .scanner import read_track
    from .config import get_settings

    root = get_settings().music_root
    path = root / track.path.lstrip("/")
    scanned = read_track(path, root)
    stat = path.stat()
    track.title = scanned.title or path.stem
    track.artist = scanned.artist
    track.album = scanned.album
    track.album_artist = scanned.album_artist or scanned.effective_album_artist
    track.disc = scanned.disc
    track.year = scanned.year
    track.file_format = path.suffix.lower().lstrip(".")
    track.file_size = stat.st_size
    track.mtime_ns = stat.st_mtime_ns
    track.has_embedded_art = scanned.has_embedded_art
    track.musicbrainz_trackid = scanned.musicbrainz_trackid


def create_search_session(db, tracks: list[Track], *, selection_kind: str,
                          folder_path: str = "", recursive: bool = True,
                          include_existing: bool = True, confirmed_large: bool = False,
                          overrides: dict[int, dict] | None = None) -> SearchSession:
    session = SearchSession(
        selection_kind=selection_kind, folder_path=folder_path, recursive=recursive,
        include_existing=include_existing, confirmed_large=confirmed_large,
        total_tracks=len(tracks), message="queued",
    )
    db.add(session)
    db.flush()
    queries: dict[tuple[QueryRole, str], ArtworkQuery] = {}
    overrides = overrides or {}
    for track in tracks:
        refresh_track(track)
        override = overrides.get(track.id, {})
        search_title = (override.get("title") or track.title).strip()
        search_artist = (override.get("artist") or track.artist).strip()
        search_album = (override.get("album") or track.album).strip()
        search_album_artist = (override.get("album_artist") or track.album_artist).strip()
        search_year = override.get("year") if override.get("year") is not None else track.year
        group = db.get(AlbumGroup, track.group_id)
        album_id = (group.musicbrainz_albumid if group else None) or ""
        release_group_id = (group.musicbrainz_releasegroupid if group else None) or ""
        track_key = track.musicbrainz_trackid or "|".join((normalize(search_artist), normalize(search_title)))
        base = album_base(search_album)
        album_key = release_group_id or album_id or "|".join((
            normalize(search_album_artist), base, str(search_year or ""),
            posixpath.dirname(posixpath.dirname(track.path)),
        ))
        tq = queries.get((QueryRole.track, track_key))
        if tq is None:
            tq = ArtworkQuery(
                session_id=session.id, role=QueryRole.track, query_key=track_key,
                artist=search_artist, title=search_title, album=search_album,
                album_artist=search_album_artist, year=search_year,
                musicbrainz_id=track.musicbrainz_trackid,
            )
            db.add(tq)
            db.flush()
            queries[(QueryRole.track, track_key)] = tq
        aq = queries.get((QueryRole.album, album_key))
        if aq is None:
            aq = ArtworkQuery(
                session_id=session.id, role=QueryRole.album, query_key=album_key,
                artist=search_album_artist, title=base or search_album,
                album=search_album, album_artist=search_album_artist, year=search_year,
                musicbrainz_id=album_id or None, release_group_id=release_group_id or None,
            )
            db.add(aq)
            db.flush()
            queries[(QueryRole.album, album_key)] = aq
        target = SearchTarget(
            session_id=session.id, track_id=track.id, track_query_id=tq.id,
            album_query_id=aq.id, fingerprint=fingerprint(track),
            search_title=search_title, search_artist=search_artist,
            search_album=search_album, search_album_artist=search_album_artist,
        )
        db.add(target)
    db.flush()
    return session


async def _check_control(session_id: int) -> None:
    while True:
        with session_scope() as db:
            status = db.get(SearchSession, session_id).status
        if status == SearchStatus.cancelled:
            raise SearchCancelled()
        if status != SearchStatus.paused:
            return
        await asyncio.sleep(1)


def _source_weight(observation: CandidateObservation) -> float:
    if observation.source == "musicbrainz" and observation.evidence_kind == "exact_id":
        return 5.0
    return _SOURCE_WEIGHT.get(observation.source, 1.0)


def _recompute_cluster(db, cluster: ArtworkCluster) -> None:
    observations = list(db.scalars(
        select(CandidateObservation).where(CandidateObservation.cluster_id == cluster.id)
    ))
    if not observations:
        return
    assets = {asset.id: asset for asset in db.scalars(
        select(ArtworkAsset).where(ArtworkAsset.id.in_([o.asset_id for o in observations]))
    )}
    representative = max((assets[o.asset_id] for o in observations), key=lambda a: a.width * a.height)
    cluster.representative_asset_id = representative.id
    best_by_source: dict[str, float] = {}
    best_raw_by_source: dict[str, float] = {}
    for observation in observations:
        contribution = _source_weight(observation) * max(0.05, observation.metadata_confidence)
        best_by_source[observation.source] = max(best_by_source.get(observation.source, 0.0), contribution)
        best_raw_by_source[observation.source] = max(
            best_raw_by_source.get(observation.source, 0.0), observation.metadata_confidence)
    independent = {(o.source, o.source_host) for o in observations if o.source_host}
    bonus = min(2.0, max(0, len(independent) - len(best_by_source)) * 0.25)
    cluster.score = round(sum(best_by_source.values()) + bonus, 3)
    cluster.source_count = len(best_by_source)
    cluster.observation_count = len(observations)
    # A source only counts as "trusted" evidence for medium/high labeling if its
    # own best match confidence clears a real bar — otherwise a single barely-
    # passing fuzzy match (classify() floors confidence at 0.05 rather than
    # rejecting it outright) was enough to earn "medium" and get swept up by
    # bulk "Approve high & medium", regardless of how weak the actual match was.
    trusted = {s for s in best_by_source if s != "google" and best_raw_by_source.get(s, 0.0) >= 0.5}
    exact = any(o.evidence_kind in ("exact_id", "exact_path") for o in observations)
    google_hosts = {o.source_host for o in observations if o.source == "google" and o.source_host}
    if exact or len(trusted) >= 2:
        cluster.confidence_label = "high"
    elif trusted or len(google_hosts) >= 3:
        cluster.confidence_label = "medium"
    else:
        cluster.confidence_label = "low"


def _persist_observation(query_id: int, candidate, fetched, confidence: float,
                         reason: str, evidence_kind: str) -> int | None:
    with session_scope() as db:
        existing = db.scalar(select(CandidateObservation.id).where(
            CandidateObservation.query_id == query_id,
            CandidateObservation.source == candidate.source,
            CandidateObservation.image_url == candidate.image_url,
        ))
        if existing:
            return existing
        asset = db.scalar(select(ArtworkAsset).where(ArtworkAsset.sha256 == fetched.sha256))
        if asset is None:
            asset = ArtworkAsset(
                sha256=fetched.sha256, phash=fetched.phash, width=fetched.width,
                height=fetched.height, cache_path=fetched.cache_path,
            )
            db.add(asset)
            db.flush()
        clusters = list(db.scalars(select(ArtworkCluster).where(ArtworkCluster.query_id == query_id)))
        chosen: ArtworkCluster | None = None
        best_distance = CLUSTER_THRESHOLD + 1
        for cluster in clusters:
            representative = db.get(ArtworkAsset, cluster.representative_asset_id)
            if representative.sha256 == asset.sha256:
                chosen = cluster
                break
            distance = hamming_distance(representative.phash, asset.phash)
            if distance <= CLUSTER_THRESHOLD and distance < best_distance:
                chosen, best_distance = cluster, distance
        if chosen is None:
            chosen = ArtworkCluster(query_id=query_id, representative_asset_id=asset.id)
            db.add(chosen)
            db.flush()
        observation = CandidateObservation(
            query_id=query_id, cluster_id=chosen.id, asset_id=asset.id,
            source=candidate.source, image_url=candidate.image_url,
            provenance_url=candidate.provenance_url,
            source_host=urlsplit(candidate.image_url).hostname or "",
            metadata_confidence=confidence, evidence_kind=evidence_kind, reason=reason,
        )
        db.add(observation)
        db.flush()
        _recompute_cluster(db, chosen)
        return observation.id


def _album_meta(db, query: ArtworkQuery) -> GroupMeta:
    target = db.scalars(select(SearchTarget).where(SearchTarget.album_query_id == query.id).limit(1)).first()
    track = db.get(Track, target.track_id)
    group = db.get(AlbumGroup, track.group_id)
    return GroupMeta(
        album=query.title, album_artist=query.album_artist, year=query.year,
        track_count=group.track_count if group else 0,
        mbid=query.musicbrainz_id, release_group_id=query.release_group_id,
        is_compilation=group.is_compilation if group else False,
        track_paths=tuple(t.path for t in group.tracks) if group else (track.path,),
    )


def _query_has_high_cluster(query_id: int) -> bool:
    with session_scope() as db:
        return bool(db.scalar(select(func.count()).select_from(ArtworkCluster).where(
            ArtworkCluster.query_id == query_id,
            ArtworkCluster.confidence_label == "high",
        )))


async def _run_query(client, query_id: int, session_id: int) -> None:
    await _check_control(session_id)
    with session_scope() as db:
        query = db.get(ArtworkQuery, query_id)
        if query.status == WorkStatus.ready:
            return
        query.status, query.progress, query.message = WorkStatus.running, 0.05, "searching trusted sources"
        role = query.role
        if role == QueryRole.album:
            meta = _album_meta(db, query)
        else:
            meta = TrackMeta(query.title, query.artist, query.album, query.year, query.musicbrainz_id)

    candidates = []
    if role == QueryRole.album:
        sources = (*TRUSTED, PLEX) if PLEX.enabled else TRUSTED
        for source in sources:
            await _check_control(session_id)
            try:
                candidates.extend(await source.find(client, meta))
            except Exception:
                continue
    else:
        candidates = await find_track_candidates(client, meta)

    total = max(1, len(candidates))
    for index, candidate in enumerate(candidates):
        await _check_control(session_id)
        if role == QueryRole.album:
            result = classify(meta, candidate.release)
        else:
            result = classify_track(
                meta, title=candidate.release.title, artist=candidate.release.artist,
                exact_identifier=candidate.extra.get("evidence_kind") == "exact_id",
            )
        if result.rejected:
            continue
        headers = PLEX._headers() if candidate.source == "plex" else None
        fetched = await fetch_and_hash(client, candidate.image_url, headers=headers)
        if fetched.ok:
            evidence = candidate.extra.get("evidence_kind", "exact_path" if candidate.source == "plex" else "search")
            _persist_observation(query_id, candidate, fetched, result.confidence, result.reason, evidence)
        with session_scope() as db:
            q = db.get(ArtworkQuery, query_id)
            q.progress = 0.1 + 0.4 * ((index + 1) / total)
            q.message = f"trusted images {index + 1}/{len(candidates)}"

    if not _query_has_high_cluster(query_id):
        await _check_control(session_id)
        with session_scope() as db:
            q = db.get(ArtworkQuery, query_id)
            q.google_used = True
            q.message = "searching Google Images"
        google_query = (
            f'"{meta.artist}" "{meta.title}" single cover art'
            if role == QueryRole.track else
            f'"{meta.album_artist}" "{meta.album}" album cover art'
        )
        async with _GOOGLE_SEMAPHORE:
            raw = await GoogleImagesSource(max_results=18).find_text(
                client, google_query, title=meta.title if role == QueryRole.track else meta.album,
                artist=meta.artist if role == QueryRole.track else meta.album_artist,
            )
        for index, candidate in enumerate(raw):
            await _check_control(session_id)
            fetched = await fetch_and_hash(client, candidate.image_url)
            if fetched.ok:
                _persist_observation(query_id, candidate, fetched, 0.5, "Google image result", "search")
            with session_scope() as db:
                q = db.get(ArtworkQuery, query_id)
                q.progress = 0.55 + 0.4 * ((index + 1) / max(1, len(raw)))
                q.message = f"Google images {index + 1}/{len(raw)}"

    with session_scope() as db:
        query = db.get(ArtworkQuery, query_id)
        query.status, query.progress, query.message = WorkStatus.ready, 1.0, "ready for review"
        query.finished_at = utcnow()


def _refresh_session_progress(session_id: int) -> None:
    with session_scope() as db:
        session = db.get(SearchSession, session_id)
        queries = list(db.scalars(select(ArtworkQuery).where(ArtworkQuery.session_id == session_id)))
        progress = sum(q.progress for q in queries) / max(1, len(queries))
        for target in db.scalars(select(SearchTarget).where(SearchTarget.session_id == session_id)):
            tq, aq = db.get(ArtworkQuery, target.track_query_id), db.get(ArtworkQuery, target.album_query_id)
            target.progress = (tq.progress + aq.progress) / 2
            if tq.status in _TERMINAL_QUERY and aq.status in _TERMINAL_QUERY:
                target.status = WorkStatus.failed if tq.status == aq.status == WorkStatus.failed else WorkStatus.ready
                target.stage = "review"
                for role, q in ((QueryRole.album, aq), (QueryRole.track, tq)):
                    best = db.scalars(select(ArtworkCluster).where(
                        ArtworkCluster.query_id == q.id).order_by(ArtworkCluster.score.desc()).limit(1)).first()
                    if best:
                        if role == QueryRole.album and target.selected_album_cluster_id is None:
                            target.selected_album_cluster_id = best.id
                        if role == QueryRole.track and target.selected_track_cluster_id is None:
                            target.selected_track_cluster_id = best.id
        session.completed_tracks = db.scalar(select(func.count()).select_from(SearchTarget).where(
            SearchTarget.session_id == session_id, SearchTarget.status == WorkStatus.ready)) or 0
        session.failed_tracks = db.scalar(select(func.count()).select_from(SearchTarget).where(
            SearchTarget.session_id == session_id, SearchTarget.status == WorkStatus.failed)) or 0
        session.progress = progress
        session.message = f"{session.completed_tracks}/{session.total_tracks} tracks ready"


async def run_search_session(session_id: int) -> str:
    with session_scope() as db:
        session = db.get(SearchSession, session_id)
        if session is None:
            raise ValueError("search session not found")
        session.status = SearchStatus.running
        session.started_at = session.started_at or utcnow()
        query_ids = list(db.scalars(select(ArtworkQuery.id).where(
            ArtworkQuery.session_id == session_id,
            ArtworkQuery.status.notin_(_TERMINAL_QUERY),
        )))
    semaphore = asyncio.Semaphore(4)
    async with new_client() as client:
        async def guarded(query_id: int):
            async with semaphore:
                try:
                    await _run_query(client, query_id, session_id)
                except SearchCancelled:
                    raise
                except Exception as exc:
                    with session_scope() as db:
                        query = db.get(ArtworkQuery, query_id)
                        query.status, query.message, query.progress = WorkStatus.failed, str(exc)[:2000], 1.0
                finally:
                    _refresh_session_progress(session_id)
        tasks = [asyncio.create_task(guarded(query_id)) for query_id in query_ids]
        try:
            await asyncio.gather(*tasks)
        except SearchCancelled:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with session_scope() as db:
                session = db.get(SearchSession, session_id)
                session.status, session.message, session.finished_at = SearchStatus.cancelled, "cancelled", utcnow()
            return "cancelled"
    _refresh_session_progress(session_id)
    with session_scope() as db:
        session = db.get(SearchSession, session_id)
        session.status = SearchStatus.review_ready
        session.progress = 1.0
        session.finished_at = utcnow()
        session.message = f"{session.completed_tracks} tracks ready; {session.failed_tracks} failed"
        return session.message


def _asset_bytes(db, cluster_id: int | None) -> tuple[bytes | None, ArtworkAsset | None]:
    if not cluster_id:
        return None, None
    cluster = db.get(ArtworkCluster, cluster_id)
    if cluster is None:
        return None, None
    asset = db.get(ArtworkAsset, cluster.representative_asset_id)
    try:
        return Path(asset.cache_path).read_bytes(), asset
    except OSError:
        return None, asset


def _safe_cover_targets(db, targets: list[SearchTarget]) -> set[int]:
    """Return one target per complete directory whose album choice agrees."""
    selected_by_dir: dict[str, list[SearchTarget]] = defaultdict(list)
    for target in targets:
        track = db.get(Track, target.track_id)
        selected_by_dir[posixpath.dirname(track.path)].append(target)
    safe: set[int] = set()
    for directory, selected in selected_by_dir.items():
        all_ids = set(db.scalars(select(Track.id).where(Track.path.like(directory.rstrip("/") + "/%"))))
        direct_ids = {track_id for track_id in all_ids
                      if posixpath.dirname(db.get(Track, track_id).path) == directory}
        selected_ids = {target.track_id for target in selected}
        album_ids = {target.selected_album_cluster_id for target in selected if target.album_approved}
        if direct_ids == selected_ids and len(album_ids) == 1 and None not in album_ids:
            safe.add(selected[0].id)
    return safe


def apply_search_session(session_id: int) -> str:
    with session_scope() as db:
        session = db.get(SearchSession, session_id)
        if session is None:
            raise ValueError("search session not found")
        session.status, session.message = SearchStatus.applying, "applying approved artwork"
        targets = list(db.scalars(select(SearchTarget).where(
            SearchTarget.session_id == session_id,
            SearchTarget.applied.is_(False),
            (SearchTarget.album_approved.is_(True)) | (SearchTarget.track_approved.is_(True)),
        )))
        cover_targets = _safe_cover_targets(db, targets)

    ok, failed = 0, 0
    for target_id in [target.id for target in targets]:
        with session_scope() as db:
            target = db.get(SearchTarget, target_id)
            track = db.get(Track, target.track_id)
            try:
                if fingerprint(track) != target.fingerprint:
                    raise ValueError("track changed after search; search again")
                album_raw, album_asset = _asset_bytes(db, target.selected_album_cluster_id if target.album_approved else None)
                track_raw, track_asset = _asset_bytes(db, target.selected_track_cluster_id if target.track_approved else None)
                if target.album_approved and album_raw is None:
                    raise ValueError("selected album image cache is missing")
                if target.track_approved and track_raw is None:
                    raise ValueError("selected track image cache is missing")
                backup, hashes, wrote_cover = apply_track_artwork(
                    track, album_raw=album_raw, track_raw=track_raw,
                    replace_album=target.album_approved, replace_track=target.track_approved,
                    write_cover_jpg=target.id in cover_targets, backup_key=target.id,
                )
                db.add(TrackWriteAudit(
                    session_id=session_id, target_id=target.id, track_id=track.id,
                    roles=",".join(role for role, approved in (("album", target.album_approved), ("track", target.track_approved)) if approved),
                    backup_dir=backup, image_sha256=hashes, wrote_cover_jpg=wrote_cover,
                ))
                target.status, target.stage, target.progress, target.applied = WorkStatus.applied, "applied", 1.0, True
                stat = (get_settings().music_root / track.path.lstrip("/")).stat()
                track.file_size, track.mtime_ns, track.has_embedded_art = stat.st_size, stat.st_mtime_ns, True
                ok += 1
            except Exception as exc:
                target.status, target.error = WorkStatus.failed, str(exc)[:2000]
                db.add(TrackWriteAudit(
                    session_id=session_id, target_id=target.id, track_id=track.id,
                    roles="", ok=False, error=str(exc)[:2000],
                ))
                failed += 1
    with session_scope() as db:
        session = db.get(SearchSession, session_id)
        session.status = SearchStatus.done if failed == 0 else SearchStatus.review_ready
        session.message = f"applied {ok} tracks; {failed} failed"
        session.finished_at = utcnow()
    return f"applied {ok} tracks; {failed} failed"
