"""Tests for engine.api.auth.dependency — auth dependency functions."""

from __future__ import annotations

import uuid

import pytest

from engine.api.auth.dependency import (
    ROLE_HIERARCHY,
    _SCOPE_HIERARCHY,
    _scope_satisfied,
    require_api_scope,
    require_role,
)


class TestRoleHierarchy:
    def test_user_lowest(self):
        assert ROLE_HIERARCHY["user"] < ROLE_HIERARCHY["developer"]

    def test_developer_middle(self):
        assert ROLE_HIERARCHY["developer"] < ROLE_HIERARCHY["admin"]

    def test_admin_highest(self):
        assert ROLE_HIERARCHY["admin"] > ROLE_HIERARCHY["user"]


class TestScopeSatisfied:
    def test_exact_scope_match(self):
        assert _scope_satisfied(["read"], "read") is True

    def test_higher_scope_satisfies_lower(self):
        assert _scope_satisfied(["trade"], "read") is True
        assert _scope_satisfied(["admin"], "read") is True
        assert _scope_satisfied(["admin"], "trade") is True

    def test_lower_scope_fails_higher(self):
        assert _scope_satisfied(["read"], "trade") is False
        assert _scope_satisfied(["read"], "admin") is False

    def test_none_scopes_denied(self):
        assert _scope_satisfied(None, "read") is False

    def test_empty_scopes_denied(self):
        assert _scope_satisfied([], "read") is False

    def test_unknown_scope_denied(self):
        assert _scope_satisfied(["unknown"], "read") is False


class TestRequireRole:
    def test_returns_callable(self):
        checker = require_role("admin")
        assert callable(checker)

    def test_require_unknown_role(self):
        checker = require_role("superadmin")
        assert callable(checker)


class TestRequireApiScope:
    def test_returns_callable(self):
        checker = require_api_scope("read")
        assert callable(checker)

    def test_unknown_scope_raises(self):
        with pytest.raises(ValueError, match="unknown scope"):
            require_api_scope("nonexistent")
