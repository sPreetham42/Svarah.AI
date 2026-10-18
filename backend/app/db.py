"""Database setup: SQLite + SQLAlchemy 2.0.

The schema stays Postgres-compatible in design (integer PKs, explicit
nullable columns, no SQLite-only types).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _create_engine(url: str) -> Engine:
    connect_args: dict[str, Any] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        # Generation steps run side by side; a writer waits for the lock
        # instead of failing with "database is locked".
        connect_args["timeout"] = 30
    return create_engine(url, connect_args=connect_args, future=True)


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _create_engine(get_settings().database_url)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, expire_on_commit=False
        )
    return _session_factory


def reset_engine() -> None:
    """Test hook: drop cached engine/session factory (dispose first)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def init_db() -> None:
    """Create all tables. Idempotent — safe on every startup."""
    from . import models  # noqa: F401  (register mappers before create_all)

    Base.metadata.create_all(bind=get_engine())
    _add_missing_columns()


#: Columns added after the first release. `create_all` only creates missing
#: tables, so an existing SQLite file needs these appended explicitly. Each
#: entry is additive and nullable, which keeps the operation safe to repeat and
#: safe to run against a database written by an older build.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("shops", "owner_uid", "VARCHAR(128)"),
    ("campaigns", "owner_uid", "VARCHAR(128)"),
    ("users", "phone", "VARCHAR(32)"),
)


def _add_missing_columns() -> None:
    """Append post-release nullable columns to an existing database."""
    engine = get_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table, column, column_type in _ADDED_COLUMNS:
            if table not in existing_tables:
                continue
            present = {col["name"] for col in inspector.get_columns(table)}
            if column in present:
                continue
            # Table and column names here are module constants, never input.
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"))


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a scoped session."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
