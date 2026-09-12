from __future__ import annotations

from collections import defaultdict
import json
import posixpath
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..dual_writer import fingerprint, read_artwork, track_path
from ..models import (
    AlbumGroup, ArtworkAsset, ArtworkCluster, ArtworkQuery, CandidateObservation, GroupState,
    Job, JobKind, QueryRole, SearchSession, SearchStatus, SearchTarget, Track, TrackWriteAudit,
    WorkStatus, utcnow,
)
from ..scanner import _easy
from ..schemas import (
    ApproveRecommendedRequest, LibraryTrackOut, SearchSelectionRequest, SelectionUpdate,
)
from ..search_service import create_search_session
from ..writer import restore_files

router = APIRouter(prefix="/api", tags=["search"])
_AUDIO = set(get_settings().audio_extensions)


def _virtual_path(raw: str | None) -> str:
    value = (raw or "/").replace("\\", "/")
    parts = [part for part in PurePosixPath(value).parts if part not in ("/", "")]
    if any(part in ("..", ".") for part in parts) or "@eaDir" in parts:
        raise HTTPException(400, "invalid music folder")
    virtual = "/" + "/".join(parts)
    root = get_settings().music_root.resolve()
    try:
        (root / virtual.lstrip("/")).resolve().relative_to(root)
    except (OSError, ValueError):
        raise HTTPException(400, "folder escapes MUSIC_ROOT")
    return virtual


def _track_dict(track: Track) -> dict:
    return LibraryTrackOut.model_validate(track).model_dump()


@router.get("/library/tracks", response_model=list[LibraryTrackOut])
def library_tracks(
    db: Session = Depends(get_session), q: str | None = None,
    folder: str | None = None, recursive: bool = True,
    limit: int = Query(100, le=500), offset: int = 0,
):
    stmt = select(Track)
    if q:
        value = f"%{q.lower()}%"
        stmt = stmt.where(or_(
            func.lower(Track.title).like(value), func.lower(Track.artist).like(value),
            func.lower(Track.album).like(value), func.lower(Track.album_artist).like(value),
            func.lower(Track.path).like(value),
        ))
    if folder is not None:
        virtual = _virtual_path(folder)
        prefix = virtual.rstrip("/") + "/"
        stmt = stmt.where(Track.path.like(prefix + "%"))
    rows = list(db.scalars(stmt.order_by(Track.path).limit(5000)))
    if folder is not None and not recursive:
        rows = [track for track in rows if posixpath.dirname(track.path) == virtual]
    return rows[offset:offset + limit]


@router.get("/library/folders")
def library_folders(parent: str = "/"):
    virtual = _virtual_path(parent)
    root = get_settings().music_root.resolve()
    real = root / virtual.lstrip("/")
    if not real.is_dir():
        raise HTTPException(404, "folder not found")
    folders = []
    try:
        entries = sorted((entry for entry in real.iterdir() if entry.is_dir() and entry.name != "@eaDir"),
                         key=lambda entry: entry.name.casefold())
    except OSError as exc:
        raise HTTPException(400, str(exc))
    for entry in entries:
        try:
            entry.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        child = (virtual.rstrip("/") + "/" + entry.name) if virtual != "/" else "/" + entry.name
        folders.append({"name": entry.name, "path": child})
    return {"parent": virtual, "folders": folders}


def _resolve_tracks(db: Session, body: SearchSelectionRequest) -> tuple[list[Track], str, str]:
    sources = sum([bool(body.track_ids), bool(body.folder_path), bool(body.album_ids)])
    if sources != 1:
        raise HTTPException(422, "provide exactly one of track_ids, folder_path, or album_ids")
    if body.track_ids:
        unique = list(dict.fromkeys(body.track_ids))
        tracks = list(db.scalars(select(Track).where(Track.id.in_(unique))))
        if len(tracks) != len(unique):
            raise HTTPException(404, "one or more tracks were not found")
        return tracks, "tracks", ""
    if body.album_ids:
        unique = list(dict.fromkeys(body.album_ids))
        groups = list(db.scalars(select(AlbumGroup).where(AlbumGroup.id.in_(unique))))
        if len(groups) != len(unique):
            raise HTTPException(404, "one or more albums were not found")
        merged_ids = {g.merged_into_id for g in groups if g.merged_into_id is not None}
        all_group_ids = {g.id for g in groups}
        if merged_ids:
            member_ids = db.scalars(
                select(AlbumGroup.id).where(AlbumGroup.merged_into_id.in_(merged_ids))
            ).all()
            all_group_ids.update(member_ids)
            groups = list(db.scalars(select(AlbumGroup).where(AlbumGroup.id.in_(all_group_ids))))
        for g in groups:
            if not (g.album or "").strip() and not g.identified_album:
                folder_name = g.common_dir or f"group #{g.id}"
                raise HTTPException(422, f"cannot search album artwork for untitled folder {folder_name!r}; use track search or identify album first")
        tracks = list(db.scalars(select(Track).where(Track.group_id.in_(all_group_ids)).order_by(Track.path)))
        return tracks, "albums", ""
    folder = _virtual_path(body.folder_path)
    prefix = folder.rstrip("/") + "/"
    tracks = list(db.scalars(select(Track).where(Track.path.like(prefix + "%")).order_by(Track.path)))
    if not body.recursive:
        tracks = [track for track in tracks if posixpath.dirname(track.path) == folder]
    return tracks, "folder", folder


@router.post("/search-sessions/preview")
def preview_search(body: SearchSelectionRequest, db: Session = Depends(get_session)):
    tracks, kind, folder = _resolve_tracks(db, body)
    overrides = {item.track_id: item.model_dump() for item in body.overrides}
    queries = set()
    for track in tracks:
        override = overrides.get(track.id, {})
        queries.add(("track", track.musicbrainz_trackid or normalize_key(
            override.get("artist") or track.artist, override.get("title") or track.title)))
        queries.add(("album", normalize_key(
            override.get("album_artist") or track.album_artist,
            override.get("album") or track.album,
            str(override.get("year") or track.year or ""))))
    return {
        "selection_kind": kind, "folder_path": folder, "track_count": len(tracks),
        "estimated_query_count": len(queries), "estimated_minutes": max(1, round(len(queries) * 8 / 60)),
        "confirmation_required": len(tracks) > 100,
    }


def normalize_key(*values: str) -> str:
    from ..matching import normalize
    return "|".join(normalize(value) for value in values)


@router.post("/search-sessions")
def start_search(body: SearchSelectionRequest, db: Session = Depends(get_session)):
    tracks, kind, folder = _resolve_tracks(db, body)
    if not tracks:
        raise HTTPException(422, "selection contains no indexed audio tracks; rescan first")
    if len(tracks) > 100 and not body.confirm_large:
        raise HTTPException(409, "selection exceeds 100 tracks; preview and confirm it first")
    try:
        session = create_search_session(
            db, tracks, selection_kind=kind, folder_path=folder,
            recursive=body.recursive, include_existing=body.include_existing,
            confirmed_large=body.confirm_large,
            overrides={item.track_id: item.model_dump() for item in body.overrides},
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(409, f"could not refresh selected track: {exc}")
    job = Job(kind=JobKind.search_session, payload=json.dumps({"session_id": session.id}))
    db.add(job)
    db.flush()
    return {"session_id": session.id, "job_id": job.id}


def _session_dict(session: SearchSession) -> dict:
    return {
        "id": session.id, "status": session.status.value, "selection_kind": session.selection_kind,
        "folder_path": session.folder_path, "recursive": session.recursive,
        "total_tracks": session.total_tracks, "completed_tracks": session.completed_tracks,
        "failed_tracks": session.failed_tracks, "progress": session.progress,
        "message": session.message, "created_at": session.created_at,
        "started_at": session.started_at, "finished_at": session.finished_at,
    }


@router.get("/search-sessions")
def list_sessions(db: Session = Depends(get_session), limit: int = Query(30, le=100)):
    return [_session_dict(row) for row in db.scalars(
        select(SearchSession).order_by(SearchSession.id.desc()).limit(limit)
    )]


@router.get("/search-sessions/{session_id}")
def get_search_session(session_id: int, include_targets: bool = True,
                       db: Session = Depends(get_session)):
    """Session header, and by default the full target payload.

    The review page polls this once a second alongside ``/albums``; ``_targets`` re-serializes
    an album's candidate clusters once per member track, so a caller that only needs the header
    should pass ``include_targets=false`` rather than pulling that payload every tick.
    """
    session = db.get(SearchSession, session_id)
    if session is None:
        raise HTTPException(404, "search session not found")
    result = _session_dict(session)
    if include_targets:
        result["targets"] = _targets(db, session_id)
    return result


@router.get("/search-sessions/{session_id}/targets")
def get_search_targets(session_id: int, db: Session = Depends(get_session)):
    if db.get(SearchSession, session_id) is None:
        raise HTTPException(404, "search session not found")
    return _targets(db, session_id)


def _clusters(db: Session, query_id: int) -> list[dict]:
    rows = list(db.scalars(select(ArtworkCluster).where(
        ArtworkCluster.query_id == query_id).order_by(ArtworkCluster.score.desc())))
    result = []
    for cluster in rows:
        observations = list(db.scalars(select(CandidateObservation).where(
            CandidateObservation.cluster_id == cluster.id).order_by(CandidateObservation.source)))
        asset = db.get(ArtworkAsset, cluster.representative_asset_id)
        result.append({
            "id": cluster.id, "score": cluster.score, "confidence_label": cluster.confidence_label,
            "source_count": cluster.source_count, "observation_count": cluster.observation_count,
            "asset_id": asset.id, "width": asset.width, "height": asset.height,
            "sources": sorted({observation.source for observation in observations}),
            "observations": [{
                "source": observation.source, "reason": observation.reason,
                "provenance_url": observation.provenance_url, "asset_id": observation.asset_id,
            } for observation in observations],
        })
    return result


def _target_dict(db: Session, target: SearchTarget) -> dict:
    track = db.get(Track, target.track_id)
    role = target.artwork_role
    gq = db.get(ArtworkQuery, target.album_query_id if role == QueryRole.album else target.track_query_id)
    selected_cluster_id = (
        target.selected_album_cluster_id if role == QueryRole.album else target.selected_track_cluster_id
    )
    approved = target.album_approved if role == QueryRole.album else target.track_approved
    return {
        "id": target.id,
        "track": _track_dict(track),
        "status": target.status.value,
        "stage": target.stage,
        "progress": target.progress,
        "message": target.message,
        "error": target.error,
        "applied": target.applied,
        "artwork_role": role.value,
        "selected_cluster_id": selected_cluster_id,
        "approved": approved,
        "current_art_url": f"/api/library/tracks/{track.id}/artwork/{role.value}",
        "search_metadata": {
            "title": target.search_title,
            "artist": target.search_artist,
            "album": target.search_album,
            "album_artist": target.search_album_artist,
        },
        "artwork_query": {
            "id": gq.id,
            "status": gq.status.value,
            "message": gq.message,
            "google_used": gq.google_used,
            "clusters": _clusters(db, gq.id),
        },
    }


def _targets(db: Session, session_id: int) -> list[dict]:
    return [
        _target_dict(db, target)
        for target in db.scalars(
            select(SearchTarget).where(SearchTarget.session_id == session_id).order_by(SearchTarget.id)
        )
    ]


@router.get("/search-sessions/{session_id}/albums")
def get_session_albums(session_id: int, db: Session = Depends(get_session)):
    session = db.get(SearchSession, session_id)
    if session is None:
        raise HTTPException(404, "search session not found")

    targets = list(db.scalars(
        select(SearchTarget).where(SearchTarget.session_id == session_id).order_by(SearchTarget.id)
    ))

    album_groups: dict[int, list[SearchTarget]] = defaultdict(list)
    singles: list[dict] = []

    for target in targets:
        if target.artwork_role == QueryRole.album:
            album_groups[target.album_query_id].append(target)
        else:
            singles.append(_target_dict(db, target))

    albums: list[dict] = []
    for album_query_id, group_targets in album_groups.items():
        aq = db.get(ArtworkQuery, album_query_id)
        tracks_for_album = [db.get(Track, t.track_id) for t in group_targets]

        # ArtworkQuery.title holds album_base(), which is normalized (lower-cased, punctuation
        # stripped) because it is a lookup key — "Dreams 3" is stored as "dreams 3". Prefer the
        # track's own album tag for display and keep the query title only as a fallback.
        title = (tracks_for_album[0].album if tracks_for_album and tracks_for_album[0].album
                 else aq.title)
        album_artist = aq.album_artist or (
            tracks_for_album[0].album_artist or tracks_for_album[0].artist if tracks_for_album else ""
        )
        year = aq.year if aq.year is not None else (tracks_for_album[0].year if tracks_for_album else None)

        directories = sorted(list({posixpath.dirname(track.path) for track in tracks_for_album if track}))
        discs = sorted(list({track.disc for track in tracks_for_album if track}))

        selected_ids = {t.selected_album_cluster_id for t in group_targets}
        selected_cluster_id = group_targets[0].selected_album_cluster_id if len(selected_ids) == 1 else None
        approved = all(t.album_approved for t in group_targets) if group_targets else False
        applied = all(t.applied for t in group_targets) if group_targets else False

        current_art_url = (
            f"/api/library/tracks/{tracks_for_album[0].id}/artwork/album"
            if tracks_for_album else ""
        )

        member_tracks = []
        for target, track in zip(group_targets, tracks_for_album):
            member_tracks.append({
                "target_id": target.id,
                "track_id": track.id,
                "title": track.title,
                "artist": track.artist,
                "disc": track.disc,
                "track_no": track.track_no,
                "path": track.path,
                "status": target.status.value,
                "stage": target.stage,
                "progress": target.progress,
                "error": target.error,
                "applied": target.applied,
            })
        member_tracks.sort(key=lambda t: (t["disc"], t["track_no"] or 0, t["path"]))

        albums.append({
            "album_query_id": aq.id,
            "title": title,
            "album_artist": album_artist,
            "year": year,
            "track_count": len(group_targets),
            "directories": directories,
            "discs": discs,
            "status": aq.status.value,
            "progress": aq.progress,
            "message": aq.message,
            "google_used": aq.google_used,
            "clusters": _clusters(db, aq.id),
            "selected_cluster_id": selected_cluster_id,
            "approved": approved,
            "applied": applied,
            "current_art_url": current_art_url,
            "tracks": member_tracks,
        })

    return {
        "albums": albums,
        "singles": singles,
    }


@router.get("/library/tracks/{track_id}/artwork/{role}")
def current_track_artwork(track_id: int, role: QueryRole, db: Session = Depends(get_session)):
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(404, "track not found")
    try:
        raw = read_artwork(track)[role.value]
    except Exception:
        raw = None
    if raw is None and role == QueryRole.album:
        real_dir = get_settings().music_root / posixpath.dirname(track.path).lstrip("/")
        for stem in ("cover", "folder", "front", "album", "albumart"):
            for extension in (".jpg", ".jpeg", ".png", ".webp"):
                candidate = real_dir / f"{stem}{extension}"
                if candidate.is_file():
                    raw = candidate.read_bytes()
                    break
            if raw is not None:
                break
    if raw is None:
        raise HTTPException(404, f"track has no managed {role.value} artwork")
    return Response(raw, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@router.get("/assets/{asset_id}/image")
def artwork_asset(asset_id: int, db: Session = Depends(get_session)):
    asset = db.get(ArtworkAsset, asset_id)
    if asset is None:
        raise HTTPException(404, "artwork asset not found")
    try:
        raw = Path(asset.cache_path).read_bytes()
    except OSError:
        raise HTTPException(410, "cached image is missing")
    return Response(raw, media_type=asset.mime_type, headers={"Cache-Control": "public, max-age=86400"})


@router.put("/search-sessions/{session_id}/targets/{target_id}/selections")
def update_selection(session_id: int, target_id: int, body: SelectionUpdate,
                     db: Session = Depends(get_session)):
    target = db.get(SearchTarget, target_id)
    if target is None or target.session_id != session_id:
        raise HTTPException(404, "search target not found")
    query_id = target.album_query_id if target.artwork_role == QueryRole.album else target.track_query_id
    if body.cluster_id is not None:
        cluster = db.get(ArtworkCluster, body.cluster_id)
        if cluster is None or cluster.query_id != query_id:
            raise HTTPException(422, f"{target.artwork_role.value} cluster does not belong to this track")
    approved = body.approved and body.cluster_id is not None
    if target.artwork_role == QueryRole.album:
        target.selected_album_cluster_id = body.cluster_id
        target.album_approved = approved
    else:
        target.selected_track_cluster_id = body.cluster_id
        target.track_approved = approved
    return {"ok": True}


@router.put("/search-sessions/{session_id}/albums/{album_query_id}/selection")
def update_album_selection(session_id: int, album_query_id: int, body: SelectionUpdate,
                           db: Session = Depends(get_session)):
    aq = db.get(ArtworkQuery, album_query_id)
    if aq is None or aq.session_id != session_id:
        raise HTTPException(404, "album query not found")
    if body.cluster_id is not None:
        cluster = db.get(ArtworkCluster, body.cluster_id)
        if cluster is None or cluster.query_id != album_query_id:
            raise HTTPException(422, "cluster does not belong to this album query")
    targets = list(db.scalars(
        select(SearchTarget).where(
            SearchTarget.session_id == session_id,
            SearchTarget.album_query_id == album_query_id,
            SearchTarget.artwork_role == QueryRole.album,
        )
    ))
    approved = body.approved and body.cluster_id is not None
    for target in targets:
        target.selected_album_cluster_id = body.cluster_id
        target.album_approved = approved
    return {"ok": True, "targets_updated": len(targets)}


@router.post("/search-sessions/{session_id}/approve-recommended")
def approve_recommended(session_id: int, body: ApproveRecommendedRequest,
                        db: Session = Depends(get_session)):
    session = db.get(SearchSession, session_id)
    if session is None:
        raise HTTPException(404, "search session not found")
    rank = {"low": 0, "medium": 1, "high": 2}
    if body.minimum_confidence not in rank:
        raise HTTPException(422, "minimum_confidence must be low, medium, or high")
    approved = 0
    for target in db.scalars(select(SearchTarget).where(SearchTarget.session_id == session_id)):
        cluster_id = (
            target.selected_album_cluster_id
            if target.artwork_role == QueryRole.album
            else target.selected_track_cluster_id
        )
        cluster = db.get(ArtworkCluster, cluster_id) if cluster_id else None
        ok = cluster is not None and rank.get(cluster.confidence_label, -1) >= rank[body.minimum_confidence]
        if target.artwork_role == QueryRole.album:
            target.album_approved = ok
        else:
            target.track_approved = ok
        approved += int(ok)
    return {"approved_targets": approved}


@router.post("/search-sessions/{session_id}/{action}")
def control_session(session_id: int, action: str, db: Session = Depends(get_session)):
    session = db.get(SearchSession, session_id)
    if session is None:
        raise HTTPException(404, "search session not found")
    if action == "pause" and session.status == SearchStatus.running:
        session.status, session.message = SearchStatus.paused, "paused"
    elif action == "resume" and session.status == SearchStatus.paused:
        session.status, session.message = SearchStatus.running, "resuming"
    elif action == "cancel" and session.status in (SearchStatus.queued, SearchStatus.running, SearchStatus.paused):
        session.status, session.message, session.finished_at = SearchStatus.cancelled, "cancelled", utcnow()
    elif action == "retry":
        for query in db.scalars(select(ArtworkQuery).where(
            ArtworkQuery.session_id == session_id, ArtworkQuery.status == WorkStatus.failed)):
            query.status, query.progress, query.message = WorkStatus.queued, 0.0, "queued for retry"
        session.status, session.message, session.finished_at = SearchStatus.queued, "queued for retry", None
        job = Job(kind=JobKind.search_session, payload=json.dumps({"session_id": session_id}))
        db.add(job)
    elif action == "apply":
        if session.status not in (SearchStatus.review_ready, SearchStatus.done):
            raise HTTPException(409, "session is not ready for review")
        # Must mirror apply_search_session's own selection exactly: an approval recorded against
        # the role that does *not* govern a target (stale rows from before artwork_role existed)
        # would otherwise pass this guard and then apply nothing.
        count = db.scalar(select(func.count()).select_from(SearchTarget).where(
            SearchTarget.session_id == session_id,
            SearchTarget.applied.is_(False),
            ((SearchTarget.artwork_role == QueryRole.album) & SearchTarget.album_approved.is_(True))
            | ((SearchTarget.artwork_role == QueryRole.track) & SearchTarget.track_approved.is_(True)),
        )) or 0
        if count == 0:
            raise HTTPException(422, "no artwork selections are approved")
        job = Job(kind=JobKind.apply_session, payload=json.dumps({"session_id": session_id}))
        db.add(job)
        session.status, session.message = SearchStatus.applying, "queued to apply"
    else:
        if action not in ("pause", "resume", "cancel", "retry", "apply"):
            raise HTTPException(404, "unknown session action")
        raise HTTPException(409, f"cannot {action} a {session.status.value} session")
    db.flush()
    return _session_dict(session)


@router.get("/track-audit")
def track_audit(db: Session = Depends(get_session), limit: int = Query(100, le=500)):
    rows = db.scalars(select(TrackWriteAudit).order_by(TrackWriteAudit.id.desc()).limit(limit))
    return [{
        "id": row.id, "session_id": row.session_id, "target_id": row.target_id,
        "track_id": row.track_id, "action": row.action, "roles": row.roles,
        "ok": row.ok, "error": row.error, "created_at": row.created_at,
        "undone_at": row.undone_at, "wrote_cover_jpg": row.wrote_cover_jpg,
    } for row in rows]


@router.post("/track-audit/{audit_id}/undo")
def undo_track_audit(audit_id: int, db: Session = Depends(get_session)):
    row = db.get(TrackWriteAudit, audit_id)
    if row is None or row.action not in ("apply", "tags"):
        raise HTTPException(404, "write audit not found")
    if row.undone_at:
        return {"restored": 0}
    if not row.backup_dir:
        raise HTTPException(409, "write has no backup")
    restored = restore_files(row.backup_dir)
    row.undone_at = utcnow()
    db.add(TrackWriteAudit(
        session_id=row.session_id, target_id=row.target_id, track_id=row.track_id,
        action="undo", roles=row.roles, backup_dir=row.backup_dir, ok=True,
    ))
    if row.action == "apply":
        if row.target_id:
            target = db.get(SearchTarget, row.target_id)
            if target:
                target.applied, target.status, target.stage = False, WorkStatus.ready, "review"
    elif row.action == "tags":
        track = db.get(Track, row.track_id)
        if track:
            p = track_path(track)
            if p.is_file():
                stat = p.stat()
                track.file_size = stat.st_size
                track.mtime_ns = stat.st_mtime_ns
                info = _easy(p)
                track.album = info.get("album") or ""
                track.album_artist = info.get("albumartist") or ""
                track.artist = info.get("artist") or ""
                new_fp = fingerprint(track)
                unapplied_targets = list(db.scalars(
                    select(SearchTarget).where(
                        SearchTarget.track_id == track.id,
                        SearchTarget.applied.is_(False),
                    )
                ))
                for tgt in unapplied_targets:
                    tgt.fingerprint = new_fp
    db.commit()
    return {"restored": restored}
