"""SQLAlchemy engine, session factory and declarative base."""

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.schema import CreateColumn

from .config import get_settings

settings = get_settings()


def _ensure_sqlite_dir(database_url: str) -> None:
    """Make sure the parent folder of a SQLite file exists."""
    if not database_url.startswith("sqlite"):
        return
    _, _, raw_path = database_url.partition("///")
    if raw_path and raw_path != ":memory:":
        Path(raw_path).expanduser().parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_dir(settings.database_url)

_connect_args: dict = {}
if settings.database_url.startswith("sqlite"):
    _connect_args["check_same_thread"] = False
elif settings.database_url.startswith("postgres"):
    # Keep TIMESTAMPTZ columns interpreted as UTC regardless of container TZ.
    _connect_args["options"] = "-c timezone=UTC"

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Declarative base for all CloudVaultX ORM models."""


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def sync_missing_columns(target_engine, metadata=None) -> list[str]:
    """Add columns that exist in the models but not yet in the database.

    ``create_all`` only creates *missing tables*; it never alters existing ones,
    so a long-lived volume (or an existing Postgres database) would otherwise
    break as soon as a model gains a column. This performs additive-only
    migration: brand new columns are appended, which is safe when they are
    nullable or carry a server-side default.

    Anything beyond that - renaming, retyping, dropping, backfilling - needs a
    real migration tool; this exists so the zero-setup deployment keeps working.
    """
    metadata = metadata if metadata is not None else Base.metadata
    inspector = inspect(target_engine)
    applied: list[str] = []

    for table in metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            if column.primary_key:
                continue
            if not column.nullable and column.server_default is None:
                # Not safely addable to a table that may already hold rows.
                raise RuntimeError(
                    f"Cannot add required column {table.name}.{column.name} without a "
                    "server_default; write a migration for this change."
                )
            ddl = str(CreateColumn(column).compile(dialect=target_engine.dialect))
            with target_engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))
            applied.append(f"{table.name}.{column.name}")

    return applied


def init_db() -> None:
    """Create all tables and add any newly introduced columns."""
    from . import models  # noqa: F401  (register mappers before create_all)

    Base.metadata.create_all(bind=engine)
    for change in sync_missing_columns(engine):
        logging.getLogger(__name__).info("Added missing column: %s", change)


def reset_database() -> None:
    """Drop and recreate every table. Only used by tests/dev tooling."""
    from . import models  # noqa: F401

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
