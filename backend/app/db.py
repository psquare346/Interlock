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
        _sqlite_patch_columns()
    # Postgres schema comes from `alembic upgrade head` — deliberately not here.


def _sqlite_patch_columns() -> None:
    """create_all adds new TABLES but never new COLUMNS to existing ones.
    For zero-setup SQLite dev, patch known additive columns in place so an
    existing punchout.db keeps working after a pull. Postgres never comes
    through here — Alembic owns it."""
    from sqlalchemy import text

    patches = {
        # table: {column: DDL type} — every additive column on a table that
        # predates it, so an old dev punchout.db keeps working after a pull.
        "tenants": {
            "po_key_hash": "VARCHAR(64)",
            "punchout_secret_hash": "VARCHAR(64)",
        },
    }
    with engine.begin() as conn:
        for table, cols in patches.items():
            existing = {
                row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))
            }
            for col, ddl in cols.items():
                if existing and col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
