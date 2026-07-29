"""SQLite locally, Postgres in production.

Schema management (SCALE.md D1): SQLite local mode still uses create_all for
zero-setup dev; any non-SQLite database is managed exclusively by Alembic
(`alembic upgrade head`) and init_db never touches its DDL.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

settings = get_settings()

_connect_args = (
    {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}
)

engine = create_engine(settings.DATABASE_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    if engine.dialect.name == "sqlite":
        Base.metadata.create_all(engine)
    # Postgres schema comes from `alembic upgrade head` — deliberately not here.


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
