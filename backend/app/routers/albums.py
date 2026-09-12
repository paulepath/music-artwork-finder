from __future__ import annotations

import json
import posixpath

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..db import get_session
from ..dual_writer import apply_track_artwork, fingerprint
from ..grouping import album_base, merge_candidate_key, suggest_merges
from ..matching import normalize
from ..models import (
    AlbumGroup,
    AlbumMerge,
    ArtworkQuery,
    AuditEntry,
    Candidate,
    GroupState,
    Job,
    JobKind,
    QueryRole,
    ScanRun,
    SearchSession,
    SearchStatus,
    SearchTarget,
    Tier,
    Track,
    TrackWriteAudit,
)
from ..http import new_client
from ..identify import guess_album_title, identify_album
from ..schemas import (
    AlbumDetail,
    AlbumSummary,
    ApplyRequest,
    CreateMergeRequest,
    DismissMergeRequest,
    IdentifyApplyRequest,
)
from ..search_service import create_search_session
from ..worker import enqueue
from .audit import _latest_undoable

router = APIRouter(prefix="/api/albums", tags=["albums"])

_TIER_RANK = {Tier.exact: 3, Tier.strong: 2, Tier.fuzzy: 1, Tier.review: 0}


def _summary(g: AlbumGroup) -> AlbumSummary:
    cands = g.candidates
    best = None
    if cands:
        best = max(cands, key=lambda c: _TIER_RANK.get(c.tier, -1)).tier.value
    s = AlbumSummary.model_validate(g)
    s.candidate_count = len(cands)
    s.best_tier = best
    s.discs = [g.disc]
    s.member_group_ids = [g.id]
    s.is_loose_tracks = bool(g.state == GroupState.multi_album_parent and not (g.album or "").strip())
    return s


def _summary_for_merged(db: Session, members: list[AlbumGroup]) -> AlbumSummary:
    first = members[0]
    merge = db.get(AlbumMerge, first.merged_into_id) if first.merged_into_id else None
    all_cands = [c for m in members for c in m.candidates]
    best = None
    if all_cands:
        best = max(all_cands, key=lambda c: _TIER_RANK.get(c.tier, -1)).tier.value

    valid_dirs = [m.common_dir for m in members if m.common_dir]
    cdir = first.common_dir
    if valid_dirs:
        try:
            cdir = posixpath.commonpath(valid_dirs) if len(set(valid_dirs)) > 1 else valid_dirs[0]
        except Exception:
            cdir = first.common_dir

    title = (merge.title if merge and merge.title else "") or first.album
    album_artist = (merge.album_artist if merge and merge.album_artist else "") or first.album_artist

    discs = sorted(list({m.disc for m in members}))
    member_ids = [m.id for m in members]
    total_tracks = sum(m.track_count for m in members)

    return AlbumSummary(
        id=first.id,
        album_artist=album_artist,
        album=title,
        disc=first.disc,
        year=first.year,
        track_count=total_tracks,
        has_embedded_art=all(m.has_embedded_art for m in members),
        has_folder_art=all(m.has_folder_art for m in members),
        state=first.state.value,
        google_enabled=any(m.google_enabled for m in members),
        is_compilation=any(m.is_compilation for m in members),
        common_dir=cdir,
        art_dupe_albums=max((m.art_dupe_albums for m in members), default=0),
        updated_at=max(m.updated_at for m in members),
        candidate_count=len(all_cands),
        best_tier=best,
        discs=discs,
        member_group_ids=member_ids,
        is_loose_tracks=any(m.state == GroupState.multi_album_parent and not (m.album or "").strip() for m in members),
    )


@router.get("", response_model=list[AlbumSummary])
def list_albums(
    db: Session = Depends(get_session),
    state: str | None = None,
    q: str | None = None,
    has_candidates: bool | None = None,
    group_by_merge: bool = True,
    exclude_loose: bool = False,
    limit: int = Query(100, le=500),
    offset: int = 0,
):
    latest_id = db.scalar(select(ScanRun.id).order_by(ScanRun.id.desc()).limit(1))
    stmt = select(AlbumGroup).options(selectinload(AlbumGroup.candidates))
    if latest_id:
        stmt = stmt.where(AlbumGroup.last_seen_scan_id == latest_id)
    if state:
        try:
            stmt = stmt.where(AlbumGroup.state == GroupState(state))
        except ValueError:
            raise HTTPException(422, f"unknown state {state!r}")
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(AlbumGroup.album).like(like) | func.lower(AlbumGroup.album_artist).like(like)
        )
    if exclude_loose:
        stmt = stmt.where(
            AlbumGroup.album != "",
            AlbumGroup.album.is_not(None),
        )

    has_title = case((func.trim(func.coalesce(AlbumGroup.album, "")) != "", 0), else_=1)

    if not group_by_merge:
        stmt = stmt.order_by(
            has_title,
            func.lower(AlbumGroup.album_artist),
            func.lower(AlbumGroup.album),
        ).limit(limit).offset(offset)
        rows = list(db.scalars(stmt))
        out = [_summary(g) for g in rows]
        if has_candidates is not None:
            out = [a for a in out if (a.candidate_count > 0) == has_candidates]
        return out

    stmt = stmt.order_by(
        has_title,
        func.lower(AlbumGroup.album_artist),
        func.lower(AlbumGroup.album),
        AlbumGroup.disc,
    )
    rows = list(db.scalars(stmt))
    seen_merges: set[int] = set()
    out = []
    for g in rows:
        if g.merged_into_id is not None:
            if g.merged_into_id in seen_merges:
                continue
            seen_merges.add(g.merged_into_id)
            members = [m for m in rows if m.merged_into_id == g.merged_into_id]
            out.append(_summary_for_merged(db, members))
        else:
            out.append(_summary(g))

    if has_candidates is not None:
        out = [a for a in out if (a.candidate_count > 0) == has_candidates]
    return out[offset : offset + limit]


@router.get("/merge-suggestions")
def merge_suggestions(db: Session = Depends(get_session)):
    latest_id = db.scalar(select(ScanRun.id).order_by(ScanRun.id.desc()).limit(1))
    stmt = select(AlbumGroup).where(
        AlbumGroup.merged_into_id.is_(None),
        AlbumGroup.merge_dismissed.is_(False),
    )
    if latest_id:
        stmt = stmt.where(AlbumGroup.last_seen_scan_id == latest_id)
    groups = list(db.scalars(stmt.order_by(AlbumGroup.album_artist, AlbumGroup.album, AlbumGroup.disc)))
    buckets = suggest_merges(groups)
    return [
        {
            "merge_key": b.merge_key,
            "title": b.title,
            "album_artist": b.album_artist,
            "groups": [
                {
                    "id": g.id,
                    "album": g.album,
                    "disc": g.disc,
                    "common_dir": g.common_dir,
                    "track_count": g.track_count,
                }
                for g in b
            ],
        }
        for b in buckets
    ]


@router.post("/merges")
def create_merge(req: CreateMergeRequest, db: Session = Depends(get_session)):
    if len(req.group_ids) < 2:
        raise HTTPException(422, "at least 2 groups required to merge")

    groups = list(db.scalars(select(AlbumGroup).where(AlbumGroup.id.in_(req.group_ids))))
    if len(groups) != len(set(req.group_ids)):
        raise HTTPException(422, "one or more group ids unknown")

    keys = {
        merge_candidate_key(g.album_artist, g.album, g.common_dir)
        for g in groups
    }
    if len(keys) == 1:
        merge_key = next(iter(keys))
    else:
        dirs = {posixpath.normpath(str(g.common_dir or "").replace("\\", "/")).rstrip("/") for g in groups}
        artists = {normalize(g.album_artist or "") for g in groups}
        if len(dirs) == 1 and len(artists) == 1 and next(iter(dirs)) and next(iter(dirs)) not in (".", "/"):
            merge_key = f"flat:{next(iter(artists)) or '?'}|||{next(iter(dirs))}"
        else:
            merge_key = f"manual:{','.join(str(gid) for gid in sorted(g.id for g in groups))}"

    first = groups[0]
    title = (req.title or "").strip()
    if not title:
        dirs = {posixpath.normpath(str(g.common_dir or "").replace("\\", "/")).rstrip("/") for g in groups}
        if len(dirs) == 1 and next(iter(dirs)) and next(iter(dirs)) not in (".", "/"):
            title = posixpath.basename(next(iter(dirs))) or album_base(first.album) or first.album
        else:
            title = album_base(first.album) or first.album

    album_artist = first.album_artist

    merge = db.scalar(select(AlbumMerge).where(AlbumMerge.merge_key == merge_key))
    if merge is None:
        merge = AlbumMerge(merge_key=merge_key, title=title, album_artist=album_artist)
        db.add(merge)
        db.flush()
    else:
        if req.title:
            merge.title = req.title.strip()
            db.flush()

    for g in groups:
        g.merged_into_id = merge.id
        g.merge_dismissed = False
    db.commit()
    return {
        "id": merge.id,
        "merge_key": merge.merge_key,
        "title": merge.title,
        "album_artist": merge.album_artist,
        "group_ids": [g.id for g in groups],
    }


@router.delete("/merges/{merge_id}")
def delete_merge(merge_id: int, db: Session = Depends(get_session)):
    merge = db.get(AlbumMerge, merge_id)
    if merge is None:
        raise HTTPException(404, "merge not found")

    members = list(db.scalars(select(AlbumGroup).where(AlbumGroup.merged_into_id == merge_id)))
    for g in members:
        g.merged_into_id = None
    db.delete(merge)
    db.commit()
    return {"ok": True, "unmerged_group_ids": [g.id for g in members]}


@router.post("/merge-suggestions/dismiss")
def dismiss_merge_suggestions(req: DismissMergeRequest, db: Session = Depends(get_session)):
    if not req.group_ids:
        return {"dismissed_count": 0}
    groups = list(db.scalars(select(AlbumGroup).where(AlbumGroup.id.in_(req.group_ids))))
    for g in groups:
        g.merge_dismissed = True
    db.commit()
    return {"dismissed_count": len(groups)}


@router.get("/{album_id}/artwork")
def current_artwork(album_id: int, db: Session = Depends(get_session)):
    """Read-only preview of the album's existing front art, for browsing."""
    from pathlib import Path
    from mutagen import File as MutagenFile
    from ..config import get_settings

    g = db.get(AlbumGroup, album_id, options=[selectinload(AlbumGroup.tracks)])
    if g is None:
        raise HTTPException(404, "album not found")
    root = get_settings().music_root
    # Prefer a real folder image; it is what MA may select ahead of embedded APIC.
    for stem in ("cover", "folder", "front", "album", "albumart"):
        for ext, mime in ((".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".png", "image/png"), (".webp", "image/webp")):
            p = root / g.common_dir.lstrip("/") / f"{stem}{ext}"
            if p.is_file():
                return Response(p.read_bytes(), media_type=mime, headers={"Cache-Control": "public, max-age=3600"})
    for t in g.tracks:
        try:
            audio = MutagenFile(str(root / t.path.lstrip("/")))
            pics = getattr(audio, "pictures", None)
            if pics:
                front = next((p for p in pics if p.type == 3), pics[0])
                return Response(bytes(front.data), media_type=getattr(front, "mime", "image/jpeg"))
            tags = getattr(audio, "tags", None)
            apics = tags.getall("APIC") if tags is not None and hasattr(tags, "getall") else []
            if apics:
                front = next((p for p in apics if getattr(p, "type", 0) == 3), apics[0])
                return Response(bytes(front.data), media_type=getattr(front, "mime", "image/jpeg"))
            covr = tags.get("covr") if tags is not None else None
            if covr:
                return Response(bytes(covr[0]), media_type="image/jpeg")
        except Exception:  # corrupt/unreadable files simply have no preview
            continue
    raise HTTPException(404, "album has no readable artwork")


@router.get("/tracks/issues")
def track_issues(db: Session = Depends(get_session), limit: int = Query(200, le=500), offset: int = 0):
    """Tracks missing embedded front art, with their album context for triage."""
    latest_id = db.scalar(select(ScanRun.id).order_by(ScanRun.id.desc()).limit(1))
    stmt = (
        select(Track, AlbumGroup)
        .join(AlbumGroup, Track.group_id == AlbumGroup.id)
        .where(Track.has_embedded_art.is_(False))
        .order_by(AlbumGroup.album_artist, AlbumGroup.album, Track.path)
        .limit(limit).offset(offset)
    )
    if latest_id:
        stmt = stmt.where(AlbumGroup.last_seen_scan_id == latest_id)
    return [{"track_id": t.id, "path": t.path, "title": t.title, "track_no": t.track_no,
             "album_id": g.id, "album": g.album, "album_artist": g.album_artist,
             "state": g.state.value} for t, g in db.execute(stmt)]


@router.post("/{album_id}/artwork-session")
def get_or_create_artwork_session(album_id: int, db: Session = Depends(get_session)):
    g = db.get(AlbumGroup, album_id)
    if g is None:
        raise HTTPException(404, "album not found")
    if not (g.album or "").strip() and not g.identified_album:
        folder_name = g.common_dir or f"group #{g.id}"
        raise HTTPException(409, f"cannot search artwork for untitled folder {folder_name!r}; identify album first")

    if g.merged_into_id:
        members = list(db.scalars(
            select(AlbumGroup).where(AlbumGroup.merged_into_id == g.merged_into_id)
        ))
        for m in members:
            if not (m.album or "").strip() and not m.identified_album:
                folder_name = m.common_dir or f"group #{m.id}"
                raise HTTPException(409, f"cannot search artwork for untitled folder {folder_name!r}; identify album first")
        group_ids = [m.id for m in members]
    else:
        group_ids = [g.id]

    tracks = list(db.scalars(
        select(Track).where(Track.group_id.in_(group_ids)).order_by(Track.disc, Track.track_no, Track.path)
    ))
    if not tracks:
        raise HTTPException(422, "album has no indexed audio tracks; rescan first")

    target_track_ids = {t.id for t in tracks}
    expected_album = (g.identified_album or g.album or "").strip()

    recent_sessions = list(db.scalars(
        select(SearchSession)
        .where(SearchSession.status == SearchStatus.review_ready)
        .order_by(SearchSession.id.desc())
        .limit(50)
    ))
    matched_session: SearchSession | None = None
    matched_aq_id: int | None = None
    for s in recent_sessions:
        if s.total_tracks == len(target_track_ids):
            s_track_ids = set(db.scalars(
                select(SearchTarget.track_id).where(SearchTarget.session_id == s.id)
            ))
            if s_track_ids == target_track_ids:
                aq = db.scalar(
                    select(ArtworkQuery)
                    .where(ArtworkQuery.session_id == s.id, ArtworkQuery.role == QueryRole.album)
                    .order_by(ArtworkQuery.id)
                )
                if aq is not None:
                    if expected_album and aq.album != expected_album:
                        continue
                    matched_session = s
                    matched_aq_id = aq.id
                    break

    if matched_session is not None and matched_aq_id is not None:
        return {
            "session_id": matched_session.id,
            "album_query_id": matched_aq_id,
        }

    try:
        session = create_search_session(db, tracks, selection_kind="albums")
    except (OSError, ValueError) as exc:
        raise HTTPException(409, f"could not refresh selected track: {exc}")

    job = Job(kind=JobKind.search_session, payload=json.dumps({"session_id": session.id}))
    db.add(job)
    db.commit()

    aq = db.scalar(
        select(ArtworkQuery)
        .where(ArtworkQuery.session_id == session.id, ArtworkQuery.role == QueryRole.album)
        .order_by(ArtworkQuery.id)
    )
    return {
        "session_id": session.id,
        "album_query_id": aq.id if aq is not None else None,
    }


@router.get("/{album_id}/identify")
async def get_album_identification(album_id: int, db: Session = Depends(get_session)):
    g = db.get(AlbumGroup, album_id)
    if g is None:
        raise HTTPException(404, "album not found")
    tracks = list(db.scalars(
        select(Track).where(Track.group_id == g.id).order_by(Track.disc, Track.track_no, Track.path)
    ))
    if not tracks:
        raise HTTPException(422, "album has no tracks to identify")
    guess = guess_album_title(g.common_dir)
    local_titles = [
        t.title.strip() if (t.title and t.title.strip()) else posixpath.splitext(posixpath.basename(t.path))[0]
        for t in tracks
    ]
    durations = [t.duration_s for t in tracks]
    artist_hint = g.album_artist or (tracks[0].artist if tracks else "")

    async with new_client() as client:
        candidates = await identify_album(
            client,
            guess=guess,
            local_titles=local_titles,
            durations=durations,
            track_count=g.track_count or len(tracks),
            artist_hint=artist_hint,
        )
    return candidates


@router.post("/{album_id}/identify")
def apply_album_identification(album_id: int, req: IdentifyApplyRequest, db: Session = Depends(get_session)):
    g = db.get(AlbumGroup, album_id)
    if g is None:
        raise HTTPException(404, "album not found")

    g.identified_album = (req.album or "").strip()
    g.identified_artist = (req.artist or "").strip()
    g.identified_mbid = req.mbid
    g.identified_release_group_id = req.release_group_id

    if g.merged_into_id:
        members = list(db.scalars(
            select(AlbumGroup).where(AlbumGroup.merged_into_id == g.merged_into_id)
        ))
        for m in members:
            m.identified_album = g.identified_album
            m.identified_artist = g.identified_artist
            m.identified_mbid = g.identified_mbid
            m.identified_release_group_id = g.identified_release_group_id
        group_ids = [m.id for m in members]
    else:
        members = [g]
        group_ids = [g.id]

    tags_written = 0
    if req.write_tags:
        tags_to_write: dict[str, str] = {}
        if g.identified_album:
            tags_to_write["album"] = g.identified_album
        if g.identified_artist:
            tags_to_write["albumartist"] = g.identified_artist
            tags_to_write["artist"] = g.identified_artist

        if tags_to_write:
            tracks = list(db.scalars(
                select(Track).where(Track.group_id.in_(group_ids))
            ))
            root = get_settings().music_root
            for track in tracks:
                backup, _, _ = apply_track_artwork(track, tags=tags_to_write)
                p = root / track.path.lstrip("/")
                stat = p.stat()
                if "album" in tags_to_write:
                    track.album = tags_to_write["album"]
                if "albumartist" in tags_to_write:
                    track.album_artist = tags_to_write["albumartist"]
                if "artist" in tags_to_write:
                    track.artist = tags_to_write["artist"]
                track.file_size = stat.st_size
                track.mtime_ns = stat.st_mtime_ns

                new_fp = fingerprint(track)
                unapplied_targets = list(db.scalars(
                    select(SearchTarget).where(
                        SearchTarget.track_id == track.id,
                        SearchTarget.applied.is_(False),
                    )
                ))
                for tgt in unapplied_targets:
                    tgt.fingerprint = new_fp

                db.add(TrackWriteAudit(
                    track_id=track.id,
                    action="tags",
                    roles="tags",
                    backup_dir=backup,
                    ok=True,
                ))
                tags_written += 1

            for m in members:
                if g.identified_album:
                    m.album = g.identified_album
                if g.identified_artist:
                    m.album_artist = g.identified_artist

    db.commit()
    return {
        "ok": True,
        "album_id": g.id,
        "identified_album": g.identified_album,
        "identified_artist": g.identified_artist,
        "identified_mbid": g.identified_mbid,
        "identified_release_group_id": g.identified_release_group_id,
        "tags_written": tags_written,
    }


@router.get("/{album_id}", response_model=AlbumDetail)
def get_album(album_id: int, db: Session = Depends(get_session)):
    g = db.get(
        AlbumGroup, album_id,
        options=[selectinload(AlbumGroup.candidates), selectinload(AlbumGroup.tracks)],
    )
    if g is None:
        raise HTTPException(404, "album not found")
    audit = list(db.scalars(
        select(AuditEntry).where(AuditEntry.group_id == album_id).order_by(AuditEntry.created_at.desc())
    ))
    detail = AlbumDetail.model_validate(g)
    base = _summary(g)
    detail.candidate_count = base.candidate_count
    detail.best_tier = base.best_tier
    detail.discs = base.discs
    detail.member_group_ids = base.member_group_ids
    detail.is_loose_tracks = base.is_loose_tracks
    if g.merged_into_id:
        members = list(db.scalars(
            select(AlbumGroup)
            .where(AlbumGroup.merged_into_id == g.merged_into_id)
            .options(selectinload(AlbumGroup.tracks))
            .order_by(AlbumGroup.disc, AlbumGroup.id)
        ))
        detail.discs = sorted(list({m.disc for m in members}))
        detail.member_group_ids = [m.id for m in members]
        detail.track_count = sum(m.track_count for m in members)
        merge = db.get(AlbumMerge, g.merged_into_id)
        if merge and merge.title:
            detail.album = merge.title
        if merge and merge.album_artist:
            detail.album_artist = merge.album_artist
        tracks = [t for m in members for t in m.tracks]
    else:
        tracks = list(g.tracks)
    detail.candidates = sorted(
        g.candidates, key=lambda c: (-_TIER_RANK.get(c.tier, -1), -c.confidence)
    )
    detail.tracks = sorted(tracks, key=lambda t: (t.disc, t.track_no or 0, t.path))
    detail.audit = audit
    return detail


@router.post("/{album_id}/find")
def find_candidates(album_id: int, db: Session = Depends(get_session)):
    if db.get(AlbumGroup, album_id) is None:
        raise HTTPException(404, "album not found")
    return {"job_id": enqueue(JobKind.find_candidates, group_id=album_id)}


@router.post("/{album_id}/enable-google")
def enable_google(album_id: int, run: bool = True, db: Session = Depends(get_session)):
    g = db.get(AlbumGroup, album_id)
    if g is None:
        raise HTTPException(404, "album not found")
    g.google_enabled = True
    db.flush()
    job_id = enqueue(JobKind.google_search, group_id=album_id) if run else None
    return {"google_enabled": True, "job_id": job_id}


@router.post("/{album_id}/skip")
def skip_album(album_id: int, db: Session = Depends(get_session)):
    g = db.get(AlbumGroup, album_id)
    if g is None:
        raise HTTPException(404, "album not found")
    g.state = GroupState.skipped
    db.flush()
    db.add(AuditEntry(group_id=album_id, action="skip", ok=True))
    return {"state": g.state.value}


@router.post("/{album_id}/apply")
def apply_candidate(album_id: int, body: ApplyRequest, db: Session = Depends(get_session)):
    group = db.get(AlbumGroup, album_id)
    if group is None:
        raise HTTPException(404, "album not found")
    if group.state != GroupState.pending_review:
        raise HTTPException(409, "album must be in pending_review; re-search after a rescan")
    cand = db.get(Candidate, body.candidate_id)
    if cand is None or cand.group_id != album_id:
        raise HTTPException(404, "candidate not found for this album")
    if cand.tier in (Tier.fuzzy, Tier.review) and not body.approved:
        raise HTTPException(400, f"{cand.tier.value} candidate requires approved=true")
    job_id = enqueue(JobKind.apply, group_id=album_id, payload={
        "candidate_id": body.candidate_id,
        "approved": body.approved,
        "write_cover_jpg": body.write_cover_jpg,
    })
    return {"job_id": job_id}


@router.post("/{album_id}/undo")
def undo_album(album_id: int, db: Session = Depends(get_session)):
    from ..writer import undo
    entry = _latest_undoable(db, album_id)
    if entry is None:
        raise HTTPException(404, "nothing to undo for this album")
    restored = undo(db, entry)
    g = db.get(AlbumGroup, album_id)
    g.state = GroupState.pending_review if g.candidates else GroupState.scanned
    return {"restored": restored, "audit_id": entry.id}


@router.post("/actions/find-all")
def find_all(include_has_art: bool = False):
    return {"job_id": enqueue(JobKind.find_candidates, payload={"include_has_art": include_has_art})}


@router.post("/actions/find-suspect")
def find_suspect():
    """Re-search every album flagged suspect_art (embedded cover shared with other albums)."""
    return {"job_id": enqueue(JobKind.find_candidates, payload={"suspect_only": True})}


@router.post("/actions/apply-exact")
def apply_all_exact(db: Session = Depends(get_session)):
    """Enqueue an apply job for every album whose single best candidate is tier=exact."""
    rows = db.scalars(
        select(AlbumGroup).options(selectinload(AlbumGroup.candidates))
        .where(AlbumGroup.state == GroupState.pending_review)
    )
    jobs = []
    for g in rows:
        exacts = [c for c in g.candidates if c.tier == Tier.exact]
        if len(exacts) >= 1:
            best = max(exacts, key=lambda c: c.confidence)
            jobs.append(enqueue(JobKind.apply, group_id=g.id,
                                payload={"candidate_id": best.id, "approved": False}))
    return {"queued": len(jobs), "job_ids": jobs}
