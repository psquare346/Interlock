"""Job polling: upload/audit endpoints return a job id; clients poll here."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Job, User
from ..services.auth import get_current_user

router = APIRouter()


def _job_out(j: Job) -> dict:
    return {
        "id": j.id,
        "kind": j.kind,
        "status": j.status.value,
        "result": j.result,
        "error": j.error,
        "attempts": j.attempts,
        "created_at": j.created_at.isoformat(),
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
    }


@router.get("")
def list_jobs(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    limit: int = 50,
):
    jobs = db.scalars(
        select(Job)
        .where(Job.tenant_id == user.tenant_id)
        .order_by(Job.created_at.desc())
        .limit(min(limit, 200))
    ).all()
    return [_job_out(j) for j in jobs]


@router.get("/{job_id}")
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = db.get(Job, job_id)
    if job is None or job.tenant_id != user.tenant_id:
        raise HTTPException(404, "Job not found")
    return _job_out(job)
