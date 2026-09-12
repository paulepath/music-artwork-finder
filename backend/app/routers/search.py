from __future__ import annotations

import json
import posixpath
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..dual_writer import read_artwork
from ..models import (
    ArtworkAsset, ArtworkCluster, ArtworkQuery, CandidateObservation, Job, JobKind,
    QueryRole, SearchSession, SearchStatus, SearchTarget, Track, TrackWriteAudit,
    WorkStatus, utcnow,
)
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
    if bool(body.track_ids) == bool(body.folder_path):
        raise HTTPException(422, "provide either track_ids or folder_path")
    if body.track_ids:
        unique = list(dict.fromkeys(body.track_ids))
        tracks = list(db.scalars(select(Track).where(Track.id.in_(unique))))
        if len(tracks) != len(unique):
            raise HTTPException(404, "one or more tracks were not found")
        return tracks, "tracks", ""
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
def get_search_session(session_id: int, db: Session = Depends(get_session)):
    session = db.get(SearchSession, session_id)
    if session is None:
        raise HTTPException(404, "search session not found")
    result = _session_dict(session)
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


def _targets(db: Session, session_id: int) -> list[dict]:
    result = []
    for target in db.scalars(select(SearchTarget).where(
        SearchTarget.session_id == session_id).order_by(SearchTarget.id)):
        track = db.get(Track, target.track_id)
        tq, aq = db.get(ArtworkQuery, target.track_query_id), db.get(ArtworkQuery, target.album_query_id)
        result.append({
            "id": target.id, "track": _track_dict(track), "status": target.status.value,
            "stage": target.stage, "progress": target.progress, "message": target.message,
            "error": target.error, "applied": target.applied,
            "selected_album_cluster_id": target.selected_album_cluster_id,
            "selected_track_cluster_id": target.selected_track_cluster_id,
            "album_approved": target.album_approved, "track_approved": target.track_approved,
            "current_album_art_url": f"/api/library/tracks/{track.id}/artwork/album",
            "current_track_art_url": f"/api/library/tracks/{track.id}/artwork/track",
            "search_metadata": {"title": target.search_title, "artist": target.search_artist,
                                "album": target.search_album, "album_artist": target.search_album_artist},
            "album_query": {"id": aq.id, "status": aq.status.value, "message": aq.message,
                            "google_used": aq.google_used, "clusters": _clusters(db, aq.id)},
            "track_query": {"id": tq.id, "status": tq.status.value, "message": tq.message,
                            "google_used": tq.google_used, "clusters": _clusters(db, tq.id)},
        })
    return result


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
    for cluster_id, query_id, label in (
        (body.album_cluster_id, target.album_query_id, "album"),
        (body.track_cluster_id, target.track_query_id, "track"),
    ):
        if cluster_id is not None:
            cluster = db.get(ArtworkCluster, cluster_id)
            if cluster is None or cluster.query_id != query_id:
                raise HTTPException(422, f"{label} cluster does not belong to this track")
    target.selected_album_cluster_id = body.album_cluster_id
    target.selected_track_cluster_id = body.track_cluster_id
    target.album_approved = body.album_approved and body.album_cluster_id is not None
    target.track_approved = body.track_approved and body.track_cluster_id is not None
    return {"ok": True}


@router.post("/search-sessions/{session_id}/approve-recommended")
def approve_recommended(session_id: int, body: ApproveRecommendedRequest,
                        db: Session = Depends(get_session)):
    rank = {"low": 0, "medium": 1, "high": 2}
    if body.minimum_confidence not in rank:
        raise HTTPException(422, "minimum_confidence must be low, medium, or high")
    approved = 0
    for target in db.scalars(select(SearchTarget).where(SearchTarget.session_id == session_id)):
        for role, cluster_id in (("album", target.selected_album_cluster_id), ("track", target.selected_track_cluster_id)):
            cluster = db.get(ArtworkCluster, cluster_id) if cluster_id else None
            ok = cluster is not None and rank[cluster.confidence_label] >= rank[body.minimum_confidence]
            if role == "album":
                target.album_approved = ok
            else:
                target.track_approved = ok
            approved += int(ok)
    return {"approved_roles": approved}


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
        count = db.scalar(select(func.count()).select_from(SearchTarget).where(
            SearchTarget.session_id == session_id,
            (SearchTarget.album_approved.is_(True)) | (SearchTarget.track_approved.is_(True)),
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
    if row is None or row.action != "apply":
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
    target = db.get(SearchTarget, row.target_id)
    if target:
        target.applied, target.status, target.stage = False, WorkStatus.ready, "review"
    return {"restored": restored}
