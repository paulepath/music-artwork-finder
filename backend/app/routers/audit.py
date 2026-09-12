from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import AlbumGroup, AuditEntry, GroupState
from ..schemas import AuditOut

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _latest_undoable(db: Session, group_id: int) -> AuditEntry | None:
    return db.scalars(
        select(AuditEntry)
        .where(AuditEntry.group_id == group_id, AuditEntry.action == "apply",
               AuditEntry.undone_at.is_(None))
        .order_by(AuditEntry.created_at.desc())
        .limit(1)
    ).first()


@router.get("", response_model=list[AuditOut])
def list_audit(
    db: Session = Depends(get_session),
    action: str | None = None,
    limit: int = Query(200, le=1000),
    offset: int = 0,
):
    stmt = select(AuditEntry).order_by(AuditEntry.created_at.desc()).limit(limit).offset(offset)
    if action:
        stmt = stmt.where(AuditEntry.action == action)
    return list(db.scalars(stmt))


@router.post("/{audit_id}/undo")
def undo_entry(audit_id: int, db: Session = Depends(get_session)):
    from ..writer import undo
    entry = db.get(AuditEntry, audit_id)
    if entry is None or entry.action != "apply":
        raise HTTPException(404, "no undoable apply entry with that id")
    if entry.undone_at is not None:
        raise HTTPException(409, "already undone")
    restored = undo(db, entry)
    g = db.get(AlbumGroup, entry.group_id)
    if g:
        g.state = GroupState.pending_review if g.candidates else GroupState.scanned
    return {"restored": restored}
