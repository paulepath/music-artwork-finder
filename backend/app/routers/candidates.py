from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Candidate, Job, JobStatus
from ..schemas import JobOut

router = APIRouter(prefix="/api", tags=["candidates", "jobs"])


@router.get("/candidates/{candidate_id}/image")
async def candidate_image(candidate_id: int, db: Session = Depends(get_session)):
    """Proxy the candidate image so the browser never talks to Google / external hosts."""
    cand = db.get(Candidate, candidate_id)
    if cand is None:
        raise HTTPException(404, "candidate not found")
    if cand.cache_path:
        try:
            with open(cand.cache_path, "rb") as fh:
                return Response(fh.read(), media_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=86400"})
        except OSError:
            pass
    # Candidate bytes are immutable evidence for the review decision.  Never
    # fetch an arbitrary, potentially changed URL at display/apply time.
    raise HTTPException(410, "candidate cache is missing; search again")


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(db: Session = Depends(get_session), active_only: bool = False, limit: int = 50):
    stmt = select(Job).order_by(Job.id.desc()).limit(limit)
    if active_only:
        stmt = stmt.where(Job.status.in_((JobStatus.queued, JobStatus.running)))
    return list(db.scalars(stmt))


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, db: Session = Depends(get_session)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job
