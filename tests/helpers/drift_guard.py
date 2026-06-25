"""Shared schema-drift guard for the test suite.

``assert_no_schema_drift`` reflects the live table behind ``engine`` and
checks it matches the ORM model's declared column shape — names, types
(compiled for the engine's dialect, so SQLite quirks like the
``JSONB`` → ``TEXT`` override are accounted for), nullability, and
``String`` lengths.

This lets tests that hand-roll a schema (or rely on ``create_all``) fail
fast when the ORM model and the materialised DB silently diverge, instead
of producing the classic "works on dev, blows up in CI" loop. Both
``test_auth.py`` and ``test_auth_e2e.py`` previously carried their own
near-identical copies of this logic; they now delegate here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String, Table, inspect

try:
    from sqlalchemy.ext.asyncio import AsyncEngine
except ImportError:  # pragma: no cover - ``sqlalchemy[asyncio]`` extra missing
    # Graceful fallback: keep the helper importable & usable with sync
    # engines even when the ``sqlalchemy[asyncio]`` extra isn't installed.
    # Async engines can't be driven without the extra, so the async branch
    # below is skipped (see the ``AsyncEngine is not None`` guard).
    AsyncEngine = None

if TYPE_CHECKING:
    from typing import Any

    from sqlalchemy.engine import Dialect


def _resolve_table(model: Any) -> Table:
    """Accept either an ORM declarative class or a raw :class:`Table`."""
    table = getattr(model, "__table__", None)
    if isinstance(table, Table):
        return table
    if isinstance(model, Table):
        return model
    raise TypeError(
        "assert_no_schema_drift expected an ORM model class or Table, "
        f"got {type(model).__name__}"
    )


def _reflect_columns(sync_conn, table_name: str) -> dict[str, dict]:
    """Reflect ``table_name``'s columns from a *sync* connection."""
    return {col["name"]: col for col in inspect(sync_conn).get_columns(table_name)}


def _compare(table: Table, reflected: dict[str, dict], dialect: Dialect) -> None:
    """Raise ``AssertionError`` (listing every mismatch) if model ≠ DB.

    Compares column **names**, **types** (model type compiled for the
    engine dialect so a JSONB column reads as ``TEXT`` on SQLite, etc.),
    **nullable** flags, and ``String`` **lengths**.
    """
    model_cols = {c.name: c for c in table.columns}
    model_names = set(model_cols)
    schema_names = set(reflected)

    mismatches: list[str] = []

    missing_in_db = model_names - schema_names
    extra_in_db = schema_names - model_names
    if missing_in_db:
        mismatches.append(f"columns in model but missing from DB: {sorted(missing_in_db)}")
    if extra_in_db:
        mismatches.append(f"columns in DB but missing from model: {sorted(extra_in_db)}")

    for name in model_names & schema_names:
        model_col = model_cols[name]
        reflected_col = reflected[name]
        model_sa_type = model_col.type
        schema_sa_type = reflected_col["type"]

        # Normalise both sides through the *same* compile path (dialect-aware)
        # so a JSONB column that materialises as TEXT on SQLite is not flagged
        # against the reflected TEXT. We deliberately avoid type-affinity
        # comparison here: JSONB and TEXT have different affinities, so that
        # route would manufacture drift.
        model_type = model_sa_type.compile(dialect=dialect)
        schema_type = schema_sa_type.compile(dialect=dialect)
        if model_type != schema_type:
            mismatches.append(
                f"column {name!r} type: model={model_type!r} db={schema_type!r}"
            )

        # ``String`` lengths are reported separately so a VARCHAR(20) vs
        # VARCHAR(50) disagreement is called out explicitly even when the
        # base type matches (as documented in the module docstring).
        if isinstance(model_sa_type, String):
            model_len = model_sa_type.length
            schema_len = getattr(schema_sa_type, "length", None)
            if model_len != schema_len:
                mismatches.append(
                    f"column {name!r} String length: model={model_len} db={schema_len}"
                )

        model_nullable = bool(model_col.nullable)
        schema_nullable = bool(reflected_col.get("nullable"))
        if model_nullable != schema_nullable:
            mismatches.append(
                f"column {name!r} nullable: model={model_nullable} db={schema_nullable}"
            )

    if mismatches:
        raise AssertionError(
            f"schema drift detected for table {table.name!r}:\n  - "
            + "\n  - ".join(mismatches)
        )


async def assert_no_schema_drift(
    model: Any, table_name: str, engine: AsyncEngine
) -> None:
    """Assert the live DB table ``table_name`` matches ``model``'s columns.

    ``model`` may be a declarative ORM class (anything exposing
    ``__table__``) or a plain :class:`~sqlalchemy.Table`. ``table_name``
    is the table to reflect from ``engine`` (which may be sync or async);
    it must agree with the model's ``__table__.name``.

    Raises:
        AssertionError: if ``table_name`` disagrees with the model's
            table name, or if any column name/type/nullable/length
            differs between the model and the reflected DB.
    """
    table = _resolve_table(model)
    if table.name != table_name:
        raise AssertionError(
            "assert_no_schema_drift: model table is "
            f"{table.name!r} but table_name={table_name!r}"
        )

    dialect = engine.dialect

    # The suite is async-first, but accept plain sync engines too so the
    # guard is reusable from either style of test. ``AsyncEngine`` is imported
    # under a try/except guard: when the ``sqlalchemy[asyncio]`` extra is
    # missing we can't handle async engines, so we skip the async check and
    # fall back to the sync reflection path.
    if AsyncEngine is not None and isinstance(engine, AsyncEngine):
        async with engine.connect() as conn:
            reflected = await conn.run_sync(_reflect_columns, table_name)
    else:
        with engine.connect() as conn:
            reflected = _reflect_columns(conn, table_name)

    _compare(table, reflected, dialect)
