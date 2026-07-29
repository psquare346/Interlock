"""Job queue: enqueue, execute, retry, dead-letter."""

import pytest

from app.models import JobStatus, Tenant
from app.services import jobs


@pytest.fixture()
def tenant(db):
    t = Tenant(id="demo", name="Demo")
    db.add(t)
    db.commit()
    return t


@pytest.fixture(autouse=True)
def _handlers():
    """Register throwaway handlers; restore the registry afterwards."""
    saved = dict(jobs._HANDLERS)
    jobs._HANDLERS.clear()

    @jobs.handler("echo")
    def _echo(db, payload):
        return {"echoed": payload.get("value")}

    calls = {"n": 0}

    @jobs.handler("flaky")
    def _flaky(db, payload):
        calls["n"] += 1
        if calls["n"] < payload.get("succeed_on", 99):
            raise RuntimeError("boom")
        return {"attempts_needed": calls["n"]}

    yield
    jobs._HANDLERS.clear()
    jobs._HANDLERS.update(saved)


def test_enqueue_and_run(db, tenant):
    job = jobs.enqueue(db, "demo", "echo", {"value": 42})
    assert job.status is JobStatus.QUEUED

    done = jobs.run_pending_once(db)
    assert done is not None and done.id == job.id
    assert done.status is JobStatus.SUCCEEDED
    assert done.result == {"echoed": 42}
    assert done.finished_at is not None

    # Queue is drained.
    assert jobs.run_pending_once(db) is None


def test_unknown_kind_rejected(db, tenant):
    with pytest.raises(ValueError):
        jobs.enqueue(db, "demo", "no_such_kind")


def test_retry_then_succeed(db, tenant):
    jobs.enqueue(db, "demo", "flaky", {"succeed_on": 2}, max_attempts=3)

    first = jobs.run_pending_once(db)
    assert first.status is JobStatus.FAILED
    assert "boom" in first.error

    second = jobs.run_pending_once(db)
    assert second.status is JobStatus.SUCCEEDED
    assert second.attempts == 2
    assert second.error is None


def test_dead_letter_after_max_attempts(db, tenant):
    jobs.enqueue(db, "demo", "flaky", {"succeed_on": 99}, max_attempts=2)

    assert jobs.run_pending_once(db).status is JobStatus.FAILED
    dead = jobs.run_pending_once(db)
    assert dead.status is JobStatus.DEAD
    # Dead jobs are not retried.
    assert jobs.run_pending_once(db) is None
