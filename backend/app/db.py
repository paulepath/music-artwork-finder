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
        "merged_into_id": "INTEGER",
        "merge_dismissed": "BOOLEAN NOT NULL DEFAULT 0",
        "identified_album": "VARCHAR(512)",
        "identified_artist": "VARCHAR(512)",
        "identified_mbid": "VARCHAR(64)",
        "identified_release_group_id": "VARCHAR(64)",
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
        "artwork_role": "VARCHAR(8) NOT NULL DEFAULT 'album'",
    },
}


def _migrate_track_write_audits(conn) -> None:
    from sqlalchemy import text
    rows = conn.execute(text("PRAGMA table_info(track_write_audits)")).fetchall()
    if not rows:
        return
    col_map = {row[1]: row for row in rows}
    # col[3] is notnull: 1 = not null, 0 = nullable
    if (col_map.get("session_id") and col_map["session_id"][3] == 1) or (
        col_map.get("target_id") and col_map["target_id"][3] == 1
    ):
        conn.execute(
            text(
                """
                CREATE TABLE track_write_audits_new (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER,
                    target_id INTEGER,
                    track_id INTEGER NOT NULL,
                    action VARCHAR(32) NOT NULL,
                    roles VARCHAR(64) NOT NULL,
                    backup_dir TEXT NOT NULL,
                    image_sha256 TEXT NOT NULL DEFAULT '',
                    wrote_cover_jpg BOOLEAN NOT NULL DEFAULT 0,
                    ok BOOLEAN NOT NULL DEFAULT 1,
                    error TEXT NOT NULL DEFAULT '',
                    created_at DATETIME NOT NULL,
                    undone_at DATETIME,
                    FOREIGN KEY(session_id) REFERENCES search_sessions (id),
                    FOREIGN KEY(target_id) REFERENCES search_targets (id),
                    FOREIGN KEY(track_id) REFERENCES tracks (id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO track_write_audits_new (
                    id, session_id, target_id, track_id, action, roles,
                    backup_dir, image_sha256, wrote_cover_jpg, ok, error,
                    created_at, undone_at
                )
                SELECT
                    id, session_id, target_id, track_id, action, roles,
                    backup_dir, image_sha256, wrote_cover_jpg, ok, error,
                    created_at, undone_at
                FROM track_write_audits
                """
            )
        )
        conn.execute(text("DROP TABLE track_write_audits"))
        conn.execute(text("ALTER TABLE track_write_audits_new RENAME TO track_write_audits"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_track_write_audits_created_at ON track_write_audits (created_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_track_write_audits_session_id ON track_write_audits (session_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_track_write_audits_track_id ON track_write_audits (track_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_track_write_audits_target_id ON track_write_audits (target_id)"))


def _add_missing_columns() -> None:
    from sqlalchemy import text
    with engine.begin() as conn:
        for table, cols in _EXPECTED_COLUMNS.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            for name, ddl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        _migrate_track_write_audits(conn)


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
