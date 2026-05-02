"""Unit tests for the export collector — gh#157."""

from __future__ import annotations

from datetime import UTC, datetime

from engine.privacy.export import _jsonify, _row_to_dict


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


class TestJsonify:
    def test_primitives_pass_through(self):
        assert _jsonify(None) is None
        assert _jsonify(True) is True
        assert _jsonify(1) == 1
        assert _jsonify(1.5) == 1.5
        assert _jsonify("x") == "x"

    def test_datetime_iso(self):
        dt = datetime(2026, 5, 3, 12, 0, 0, tzinfo=UTC)
        assert _jsonify(dt).startswith("2026-05-03T12:00:00")

    def test_list_recurses(self):
        out = _jsonify([1, "x", None])
        assert out == [1, "x", None]

    def test_dict_recurses_with_str_keys(self):
        out = _jsonify({1: "v"})
        assert out == {"1": "v"}

    def test_unknown_falls_back_to_str(self):
        class _Foo:
            def __str__(self) -> str:
                return "FOO"

        assert _jsonify(_Foo()) == "FOO"


class TestRowToDict:
    def test_includes_all_columns(self):
        row = _FakeRow(id="abc", name="Alice", password_hash="secret")
        out = _row_to_dict(row)
        assert out == {"id": "abc", "name": "Alice", "password_hash": "secret"}

    def test_deny_list_excludes_columns(self):
        row = _FakeRow(id="abc", name="Alice", password_hash="secret")
        out = _row_to_dict(row, deny=frozenset({"password_hash"}))
        assert "password_hash" not in out
        assert out == {"id": "abc", "name": "Alice"}

    def test_datetime_serialised(self):
        row = _FakeRow(created_at=datetime(2026, 5, 3, tzinfo=UTC))
        out = _row_to_dict(row)
        assert out["created_at"].startswith("2026-05-03")
