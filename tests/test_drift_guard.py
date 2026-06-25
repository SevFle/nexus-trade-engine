"""Unit tests for the shared schema-drift guard helper.

These pin the helper's contract directly (a match passes; a drift fails
with a clear message) so that the ``test_auth*.py`` consumers can rely on
it without each re-asserting the behaviour.
"""

from __future__ import annotations

import pytest
from sqlalchemy import (
    JSONB,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
)
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.compiler import compiles

from tests.helpers.drift_guard import assert_no_schema_drift

# SQLite has no JSONB; mirror the conftest override so create_all works.
compiles(JSONB, "sqlite")(lambda type_, compiler, **kw: "TEXT")


def _materialised_widget_engine() -> object:
    """A sync SQLite engine with a single ``widgets`` table created."""
    md = MetaData()
    Table(
        "widgets",
        md,
        Column("id", Integer, primary_key=True),
        Column("name", String(50), nullable=False),
        Column("notes", String, nullable=True),
    )
    engine = create_engine("sqlite://")
    md.create_all(engine, tables=[md.tables["widgets"]])
    return engine


async def test_no_drift_when_model_matches_db() -> None:
    """A model declared identically to the materialised table passes."""
    engine = _materialised_widget_engine()
    try:
        md = MetaData()
        table = Table(
            "widgets",
            md,
            Column("id", Integer, primary_key=True),
            Column("name", String(50), nullable=False),
            Column("notes", String, nullable=True),
        )
        await assert_no_schema_drift(table, "widgets", engine)
    finally:
        engine.dispose()


async def test_no_drift_works_with_async_engine() -> None:
    """The helper drives reflection through an async engine."""
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        md = MetaData()
        real = Table(
            "widgets",
            md,
            Column("id", Integer, primary_key=True),
            Column("name", String(50), nullable=False),
        )
        async with engine.begin() as conn:
            await conn.run_sync(md.create_all, tables=[real])

        await assert_no_schema_drift(real, "widgets", engine)
    finally:
        await engine.dispose()


async def test_drift_detected_on_type_and_length_and_nullable() -> None:
    """Type, length, and nullable disagreements are all reported."""
    engine = _materialised_widget_engine()
    try:
        drift_md = MetaData()
        wrong = Table(
            "widgets",
            drift_md,
            Column("id", Integer, primary_key=True),
            # real table is String(50) NOT NULL; model claims the opposite.
            Column("name", String(20), nullable=True),
            Column("notes", String, nullable=True),
        )
        with pytest.raises(AssertionError) as exc_info:
            await assert_no_schema_drift(wrong, "widgets", engine)

        message = str(exc_info.value)
        assert "'name'" in message
        assert "type" in message
        assert "String length" in message
        assert "nullable" in message
    finally:
        engine.dispose()


async def test_drift_detected_on_missing_and_extra_columns() -> None:
    engine = _materialised_widget_engine()
    try:
        drift_md = MetaData()
        # Drops `notes`, adds a phantom `colour`.
        wrong = Table(
            "widgets",
            drift_md,
            Column("id", Integer, primary_key=True),
            Column("name", String(50), nullable=False),
            Column("colour", String(10), nullable=False),
        )
        with pytest.raises(AssertionError) as exc_info:
            await assert_no_schema_drift(wrong, "widgets", engine)

        message = str(exc_info.value)
        assert "notes" in message  # missing from model
        assert "colour" in message  # extra in model
    finally:
        engine.dispose()


async def test_table_name_mismatch_raises() -> None:
    engine = _materialised_widget_engine()
    try:
        md = MetaData()
        table = Table("widgets", md, Column("id", Integer, primary_key=True))
        with pytest.raises(AssertionError, match="table_name"):
            await assert_no_schema_drift(table, "widgets_typo", engine)
    finally:
        engine.dispose()


async def test_non_model_argument_raises_typeerror() -> None:
    engine = _materialised_widget_engine()
    try:
        with pytest.raises(TypeError):
            await assert_no_schema_drift("not-a-model", "widgets", engine)
    finally:
        engine.dispose()


async def test_jsonb_compiles_to_text_on_sqlite_no_drift() -> None:
    """A JSONB model column materialises as TEXT on SQLite and must not
    be flagged as drift (regression guard for the compile-override path)."""
    engine = _materialised_widget_engine()
    try:
        drift_md = MetaData()
        Table(
            "widgets",
            drift_md,
            Column("id", Integer, primary_key=True),
            Column("name", String(50), nullable=False),
            Column("notes", String, nullable=True),
            # Add a JSONB column that the materialised DB doesn't have so
            # we instead verify the *type comparison* path on a fresh table.
        )
        # Materialise a fresh table with a JSONB column and compare to itself.
        engine2 = create_async_engine("sqlite+aiosqlite://")
        try:
            md2 = MetaData()
            real = Table(
                "gadgets",
                md2,
                Column("id", Integer, primary_key=True),
                Column("payload", JSONB, nullable=True),
            )
            async with engine2.begin() as conn:
                await conn.run_sync(md2.create_all, tables=[real])
            await assert_no_schema_drift(real, "gadgets", engine2)
        finally:
            await engine2.dispose()
    finally:
        engine.dispose()
