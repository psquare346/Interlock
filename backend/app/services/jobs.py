"""Durable background jobs (SCALE.md D5).

The seam: callers only ever `enqueue()` and poll the job row; handlers are
registered by kind. Today the executor is a daemon thread polling the jobs
table; at scale it becomes separate worker containers (Redis-backed) with no
change to callers or handlers.

Handler contract: fn(db, payload: dict) -> dict | None. The return value is
stored on the job as `result`. Raising marks the attempt failed; the job
retries until max_attempts, then goes DEAD.
"""

from __future__ import annotations

import threading
import traceback
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Job, JobStatus

_HANDLERS: dict[str, Callable[[Session, dict], dict | None]] = {}


def handler(kind: str):
    """Register a job handler: @handler("leakage_audit")."""

    def register(fn: Callable[[Session, dict], dict | None]):
        _HANDLERS[kind] = fn
        return fn

    return register


def enqueue(
    db: Session,
    tenant_id: str,
    kind: str,
    payload: dict | None = None,
    *,
    created_by: str | None = None,
    max_attempts: int = 3,
) -> Job:
    if kind not in _HANDLERS:
        raise ValueError(f"No handler registered for job kind {kind!r}")
    job = Job(
        tenant_id=tenant_id,
        kind=kind,
        payload=payload or {},
        created_by=created_by,
        max_attempts=max_attempts,
    )
    db.add(job)
    db.commit()
    return job


def _claim_next(db: Session) -> Job | None:
    """Claim the oldest runnable job. skip_locked makes this safe with many
    workers on Postgres; SQLite is single-writer so contention can't occur."""
    query = (
        select(Job)
        .where(Job.status.in_([JobStatus.QUEUED, JobStatus.FAILED]))
        .order_by(Job.created_at)
        .limit(1)
    )
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    job = db.scalars(query).first()
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.attempts += 1
    job.started_at = datetime.now(timezone.utc)
    db.commit()
    return job


def run_pending_once(db: Session) -> Job | None:
    """Claim and execute a single job. Returns it, or None if queue is empty.
    Called by the worker loop; also callable directly from tests."""
    job = _claim_next(db)
    if job is None:
        return None
    try:
        result = _HANDLERS[job.kind](db, job.payload or {})
        job.status = JobStatus.SUCCEEDED
        job.result = result
        job.error = None
    except Exception:
        db.rollback()
        job.error = traceback.format_exc(limit=20)
        job.status = (
            JobStatus.DEAD if job.attempts >= job.max_attempts else JobStatus.FAILED
        )
    job.finished_at = datetime.now(timezone.utc)
    db.commit()
    return job


class Worker:
    """In-process worker loop. Started from the app lifespan; drains the queue
    then sleeps poll_seconds. One per process is enough until Phase 2."""

    def __init__(self, session_factory, poll_seconds: float = 1.0):
        self._session_factory = session_factory
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="job-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            db = self._session_factory()
            try:
                while run_pending_once(db) is not None:
                    if self._stop.is_set():
                        break
            except Exception:
                # The loop must survive anything (e.g. DB briefly down).
                db.rollback()
            finally:
                db.close()
            self._stop.wait(self._poll)
