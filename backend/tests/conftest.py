import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base

# SQLite in-memory by default; CI also runs the whole suite against Postgres
# by setting TEST_DATABASE_URL (SCALE.md D2) — same tests, second dialect.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:")

# The operator key API tests provision tenants with.
TEST_OPERATOR_KEY = "test-operator-key-for-suite"


def _engine():
    if TEST_DATABASE_URL.startswith("sqlite"):
        # StaticPool: one shared in-memory DB across every connection the app
        # checks out during a test — without it each connection gets its own
        # empty :memory: database.
        return create_engine(
            TEST_DATABASE_URL,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    return create_engine(TEST_DATABASE_URL)


@pytest.fixture()
def db():
    engine = _engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def client(monkeypatch):
    """API-level TestClient on a fresh database, with the app's get_db
    dependency overridden and OPERATOR_KEY configured. Use for end-to-end
    tests that must exercise the real HTTP surface (auth headers, forms,
    redirects) rather than service functions."""
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.db import get_db
    from app.main import app

    monkeypatch.setenv("OPERATOR_KEY", TEST_OPERATOR_KEY)
    get_settings.cache_clear()

    engine = _engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _get_db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)
        get_settings.cache_clear()
        engine.dispose()
