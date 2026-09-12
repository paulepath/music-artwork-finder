from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import AlbumGroup, GroupState, Job, JobKind, JobStatus, ScanRun
from ..schemas import StatsOut
from ..worker import enqueue

router = APIRouter(prefix="/api", tags=["scan"])


@router.post("/scan")
def start_scan():
    return {"job_id": enqueue(JobKind.scan)}


@router.get("/scan/latest")
def latest_scan(db: Session = Depends(get_session)):
    row = db.scalars(select(ScanRun).order_by(ScanRun.id.desc()).limit(1)).first()
    if row is None:
        return {"scan": None}
    return {"scan": {
        "id": row.id,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "files_seen": row.files_seen,
        "groups_total": row.groups_total,
        "groups_missing_art": row.groups_missing_art,
    }}


@router.get("/stats", response_model=StatsOut)
def stats(db: Session = Depends(get_session)):
    latest_id = db.scalar(select(ScanRun.id).order_by(ScanRun.id.desc()).limit(1))
    current = AlbumGroup.last_seen_scan_id == latest_id if latest_id else False
    by_state = {
        s.value: db.scalar(select(func.count()).select_from(AlbumGroup).where(current, AlbumGroup.state == s))
        for s in GroupState
    }
    total = db.scalar(select(func.count()).select_from(AlbumGroup).where(current)) or 0
    missing = db.scalar(
        select(func.count()).select_from(AlbumGroup)
        .where(current, ~(AlbumGroup.has_embedded_art & AlbumGroup.has_folder_art))
    ) or 0
    last_scan = db.scalar(select(func.max(ScanRun.finished_at)))
    active = db.scalar(
        select(func.count()).select_from(Job)
        .where(Job.status.in_((JobStatus.queued, JobStatus.running)))
    ) or 0
    return StatsOut(groups_total=total, by_state=by_state, missing_art=missing,
                    last_scan=last_scan, active_jobs=active)
