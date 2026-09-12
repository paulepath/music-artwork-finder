from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..db import get_session
from ..models import AlbumGroup, AuditEntry, Candidate, GroupState, JobKind, ScanRun, Tier, Track
from ..schemas import AlbumDetail, AlbumSummary, ApplyRequest
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
    return s


@router.get("", response_model=list[AlbumSummary])
def list_albums(
    db: Session = Depends(get_session),
    state: str | None = None,
    q: str | None = None,
    has_candidates: bool | None = None,
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
    stmt = stmt.order_by(AlbumGroup.album_artist, AlbumGroup.album).limit(limit).offset(offset)
    rows = list(db.scalars(stmt))
    out = [_summary(g) for g in rows]
    if has_candidates is not None:
        out = [a for a in out if (a.candidate_count > 0) == has_candidates]
    return out


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
    detail.candidates = sorted(
        g.candidates, key=lambda c: (-_TIER_RANK.get(c.tier, -1), -c.confidence)
    )
    detail.tracks = sorted(g.tracks, key=lambda t: (t.track_no or 0, t.path))
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
