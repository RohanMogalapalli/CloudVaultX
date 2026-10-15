"""Tests for the additive schema sync.

`Base.metadata.create_all()` never alters an existing table, so a deployment
with a long-lived volume breaks the moment a model gains a column (which is
exactly what happened when the ``is_admin`` column was introduced).
``sync_missing_columns`` closes that gap for additive changes.
"""

from __future__ import annotations

import pytest
from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
    text,
)

from app.database import sync_missing_columns


def _legacy_engine(tmp_path):
    """A database that predates the newest columns."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    legacy = MetaData()
    Table(
        "widgets",
        legacy,
        Column("id", Integer, primary_key=True),
        Column("name", String(50), nullable=False),
    )
    legacy.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO widgets (name) VALUES ('existing row')"))
    return engine


def _current_metadata():
    metadata = MetaData()
    Table(
        "widgets",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(50), nullable=False),
        Column("label", String(50), nullable=True),
        Column("flagged", Boolean, nullable=False, server_default=text("false")),
    )
    return metadata


def test_adds_columns_that_are_missing(tmp_path):
    engine = _legacy_engine(tmp_path)
    applied = sync_missing_columns(engine, _current_metadata())

    assert set(applied) == {"widgets.label", "widgets.flagged"}

    columns = {column["name"] for column in inspect(engine).get_columns("widgets")}
    assert {"id", "name", "label", "flagged"} <= columns


def test_existing_rows_get_the_server_default(tmp_path):
    engine = _legacy_engine(tmp_path)
    sync_missing_columns(engine, _current_metadata())

    with engine.connect() as connection:
        row = connection.execute(text("SELECT name, flagged FROM widgets")).one()
    assert row.name == "existing row"
    assert bool(row.flagged) is False


def test_is_idempotent(tmp_path):
    engine = _legacy_engine(tmp_path)
    assert sync_missing_columns(engine, _current_metadata())
    assert sync_missing_columns(engine, _current_metadata()) == []


def test_ignores_tables_that_do_not_exist_yet(tmp_path):
    engine = _legacy_engine(tmp_path)
    metadata = MetaData()
    Table(
        "widgets",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(50), nullable=False),
    )
    Table("brand_new", metadata, Column("id", Integer, primary_key=True))

    # create_all handles genuinely new tables; the sync must skip them.
    assert sync_missing_columns(engine, metadata) == []
    assert not inspect(engine).has_table("brand_new")


def test_refuses_to_add_a_required_column_without_a_default(tmp_path):
    engine = _legacy_engine(tmp_path)
    metadata = MetaData()
    Table(
        "widgets",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("required_thing", String(20), nullable=False),
    )

    with pytest.raises(RuntimeError, match="required_thing"):
        sync_missing_columns(engine, metadata)


def test_real_schema_is_already_in_sync():
    """Our own database should need no changes after init_db()."""
    from app import models  # noqa: F401
    from app.database import Base, engine, init_db

    init_db()
    assert sync_missing_columns(engine, Base.metadata) == []
