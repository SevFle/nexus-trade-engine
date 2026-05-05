"""Tests for engine.privacy.export — data export collector."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from engine.privacy.export import _jsonify, _row_to_dict


class TestJsonify:
    def test_none(self):
        assert _jsonify(None) is None

    def test_bool(self):
        assert _jsonify(True) is True
        assert _jsonify(False) is False

    def test_int(self):
        assert _jsonify(42) == 42

    def test_float(self):
        assert _jsonify(3.14) == 3.14

    def test_str(self):
        assert _jsonify("hello") == "hello"

    def test_datetime(self):
        dt = datetime(2024, 1, 15, 12, 30, tzinfo=UTC)
        assert _jsonify(dt) == "2024-01-15T12:30:00+00:00"

    def test_list(self):
        assert _jsonify([1, "two", None]) == [1, "two", None]

    def test_dict(self):
        assert _jsonify({"a": 1}) == {"a": 1}

    def test_uuid_stringified(self):
        uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        assert _jsonify(uid) == str(uid)

    def test_decimal_stringified(self):
        assert _jsonify(Decimal("10.50")) == "10.50"

    def test_nested_structures(self):
        result = _jsonify({"items": [1, {"nested": True}]})
        assert result == {"items": [1, {"nested": True}]}


class TestRowToDict:
    def test_basic_row(self):
        row = MagicMock()
        col1 = MagicMock()
        col1.name = "id"
        col2 = MagicMock()
        col2.name = "name"
        row.__table__ = MagicMock()
        row.__table__.columns = [col1, col2]
        row.id = uuid.UUID("12345678-1234-5678-1234-567812345678")
        row.name = "test"
        result = _row_to_dict(row)
        assert result["id"] == str(row.id)
        assert result["name"] == "test"

    def test_deny_columns_excluded(self):
        row = MagicMock()
        col1 = MagicMock()
        col1.name = "password_hash"
        col2 = MagicMock()
        col2.name = "email"
        row.__table__ = MagicMock()
        row.__table__.columns = [col1, col2]
        row.password_hash = "secret"
        row.email = "test@test.com"
        result = _row_to_dict(row, deny=frozenset({"password_hash"}))
        assert "password_hash" not in result
        assert result["email"] == "test@test.com"

    def test_none_value_included(self):
        row = MagicMock()
        col = MagicMock()
        col.name = "optional_field"
        row.__table__ = MagicMock()
        row.__table__.columns = [col]
        row.optional_field = None
        result = _row_to_dict(row)
        assert result["optional_field"] is None

    def test_datetime_value_converted(self):
        row = MagicMock()
        col = MagicMock()
        col.name = "created_at"
        row.__table__ = MagicMock()
        row.__table__.columns = [col]
        row.created_at = datetime(2024, 1, 1, tzinfo=UTC)
        result = _row_to_dict(row)
        assert isinstance(result["created_at"], str)
