"""Tests for engine.privacy.export — helper functions and collect_user_data error path."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from engine.privacy.export import _jsonify, _row_to_dict, collect_user_data


class TestCollectUserDataErrorPath:
    async def test_raises_for_missing_user(self, db_session: AsyncSession):
        missing = uuid.uuid4()
        with pytest.raises(LookupError, match="user not found"):
            await collect_user_data(db_session, missing)


class TestJsonifyExtended:
    def test_primitives_pass_through(self):
        assert _jsonify(None) is None
        assert _jsonify(True) is True
        assert _jsonify(1) == 1
        assert _jsonify(1.5) == 1.5
        assert _jsonify("x") == "x"

    def test_datetime_iso(self):
        dt = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)
        assert _jsonify(dt).startswith("2026-05-03T12:00:00")

    def test_nested_structures(self):
        nested = {"a": [1, {"b": datetime(2026, 1, 1)}]}
        result = _jsonify(nested)
        assert isinstance(result["a"][1]["b"], str)

    def test_list_recurses(self):
        assert _jsonify([1, "x", None]) == [1, "x", None]

    def test_dict_recurses_with_str_keys(self):
        assert _jsonify({1: "v"}) == {"1": "v"}

    def test_empty_list(self):
        assert _jsonify([]) == []

    def test_empty_dict(self):
        assert _jsonify({}) == {}

    def test_bool_not_confused_with_int(self):
        assert _jsonify(True) is True
        assert _jsonify(False) is False

    def test_unknown_falls_back_to_str(self):
        class _Foo:
            def __str__(self) -> str:
                return "FOO"

        assert _jsonify(_Foo()) == "FOO"


class TestRowToDictExtended:
    def test_includes_all_columns(self):
        class _FakeColumn:
            def __init__(self, name: str) -> None:
                self.name = name

        class _FakeTable:
            def __init__(self, columns: list[str]) -> None:
                self.columns = [_FakeColumn(c) for c in columns]

        class _FakeRow:
            def __init__(self, **fields) -> None:
                self.__table__ = _FakeTable(list(fields.keys()))
                for k, v in fields.items():
                    setattr(self, k, v)

        row = _FakeRow(id="abc", name="Alice", password_hash="secret")
        out = _row_to_dict(row)
        assert out == {"id": "abc", "name": "Alice", "password_hash": "secret"}

    def test_deny_list_excludes_columns(self):
        class _FakeColumn:
            def __init__(self, name: str) -> None:
                self.name = name

        class _FakeTable:
            def __init__(self, columns: list[str]) -> None:
                self.columns = [_FakeColumn(c) for c in columns]

        class _FakeRow:
            def __init__(self, **fields) -> None:
                self.__table__ = _FakeTable(list(fields.keys()))
                for k, v in fields.items():
                    setattr(self, k, v)

        row = _FakeRow(id="abc", name="Alice", password_hash="secret")
        out = _row_to_dict(row, deny=frozenset({"password_hash"}))
        assert "password_hash" not in out
        assert out == {"id": "abc", "name": "Alice"}

    def test_none_value_included(self):
        class _FakeRow:
            class __table__:
                columns = [type("C", (), {"name": "field"})]
            field = None

        result = _row_to_dict(_FakeRow())
        assert result["field"] is None

    def test_uuid_stringified(self):
        class _FakeRow:
            class __table__:
                columns = [type("C", (), {"name": "uid"})]
            uid = uuid.UUID("12345678-1234-1234-1234-123456789abc")

        result = _row_to_dict(_FakeRow())
        assert isinstance(result["uid"], str)

    def test_datetime_serialised(self):
        class _FakeRow:
            class __table__:
                columns = [type("C", (), {"name": "created_at"})]
            created_at = datetime(2026, 5, 3, tzinfo=UTC)

        out = _row_to_dict(_FakeRow())
        assert out["created_at"].startswith("2026-05-03")
