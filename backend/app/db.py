"""SQLite engine / session factory. One file DB under the data dir."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

_settings = get_settings()
_settings.ensure_dirs()

engine = create_engine(
    f"sqlite:///{_settings.db_path}",
    future=True,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _rec):  # pragma: no cover - trivial
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns()


# lightweight forward-only migration for SQLite (no Alembic).
_EXPECTED_COLUMNS = {
    "album_groups": {
        "art_hash": "VARCHAR(64)",
        "art_dupe_albums": "INTEGER NOT NULL DEFAULT 0",
    },
    "candidates": {
        "group_fingerprint": "VARCHAR(64) NOT NULL DEFAULT ''",
    },
    "tracks": {
        "artist": "VARCHAR(512) NOT NULL DEFAULT ''",
        "album": "VARCHAR(512) NOT NULL DEFAULT ''",
        "album_artist": "VARCHAR(512) NOT NULL DEFAULT ''",
        "disc": "INTEGER NOT NULL DEFAULT 1",
        "year": "INTEGER",
        "file_format": "VARCHAR(16) NOT NULL DEFAULT ''",
        "file_size": "INTEGER NOT NULL DEFAULT 0",
        "mtime_ns": "INTEGER NOT NULL DEFAULT 0",
    },
    # These overrides were added after the first v2 development database was
    # created. Keeping them here also makes interrupted/partial upgrades safe.
    "search_targets": {
        "search_title": "VARCHAR(512) NOT NULL DEFAULT ''",
        "search_artist": "VARCHAR(512) NOT NULL DEFAULT ''",
        "search_album": "VARCHAR(512) NOT NULL DEFAULT ''",
        "search_album_artist": "VARCHAR(512) NOT NULL DEFAULT ''",
    },
}


def _add_missing_columns() -> None:
    from sqlalchemy import text
    with engine.begin() as conn:
        for table, cols in _EXPECTED_COLUMNS.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            for name, ddl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s
