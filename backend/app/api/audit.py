"""Leakage audit endpoints: upload PO history → background job → report.

The upload returns a job id immediately (SCALE.md D5 — big files never run
inside the request); clients poll /api/jobs/{id} and download the detail CSV
here once the job succeeds."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Job, JobStatus, User
from ..services import storage
from ..services.auth import get_current_user
from ..services.jobs import enqueue

router = APIRouter()

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # a decade of PO lines fits well under this


@router.post("/upload", status_code=201)
def upload_history(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File larger than 50 MB — split the export")
    if not data.strip():
        raise HTTPException(422, "The file is empty")

    audit_id = uuid.uuid4().hex
    key = storage.put(f"{user.tenant_id}/audit-uploads/{audit_id}.csv", data)
    job = enqueue(
        db, user.tenant_id, "leakage_audit",
        {
            "storage_key": key,
            "tenant_id": user.tenant_id,
            "filename": file.filename,
            "job_id": audit_id,
        },
        created_by=user.id,
    )
    return {"job_id": job.id, "poll": f"/api/jobs/{job.id}"}


@router.get("/{job_id}/report")
def download_report(
    job_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.get(Job, job_id)
    if job is None or job.tenant_id != user.tenant_id or job.kind != "leakage_audit":
        raise HTTPException(404, "Audit not found")
    if job.status is not JobStatus.SUCCEEDED:
        raise HTTPException(409, f"Audit is {job.status.value} — report not ready")
    report_key = (job.result or {}).get("report_key")
    if not report_key or not storage.exists(report_key):
        raise HTTPException(404, "Report file missing")
    return Response(
        storage.get(report_key),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leakage-report.csv"},
    )
