"""Unit tests for the API-key scope hierarchy and gating helper (gh#86)."""

from __future__ import annotations

import pytest

from engine.api.auth.dependency import _scope_satisfied, require_api_scope


class TestScopeSatisfied:
    def test_admin_satisfies_everything(self):
        assert _scope_satisfied(["admin"], "read") is True
        assert _scope_satisfied(["admin"], "trade") is True
        assert _scope_satisfied(["admin"], "admin") is True

    def test_trade_satisfies_read_and_trade(self):
        assert _scope_satisfied(["trade"], "read") is True
        assert _scope_satisfied(["trade"], "trade") is True
        assert _scope_satisfied(["trade"], "admin") is False

    def test_read_satisfies_only_read(self):
        assert _scope_satisfied(["read"], "read") is True
        assert _scope_satisfied(["read"], "trade") is False
        assert _scope_satisfied(["read"], "admin") is False

    def test_empty_satisfies_nothing(self):
        assert _scope_satisfied([], "read") is False
        assert _scope_satisfied(None, "read") is False

    def test_unknown_scope_in_grant_does_not_help(self):
        # An unrecognised scope must not satisfy any required level.
        assert _scope_satisfied(["wizard"], "read") is False

    def test_multiple_scopes_use_max(self):
        assert _scope_satisfied(["read", "trade"], "trade") is True
        assert _scope_satisfied(["read", "trade"], "admin") is False
        assert _scope_satisfied(["read", "admin"], "admin") is True


class TestRequireApiScopeFactory:
    def test_unknown_required_scope_raises(self):
        with pytest.raises(ValueError):
            require_api_scope("wizard")

    def test_factory_returns_callable_per_scope(self):
        for s in ("read", "trade", "admin"):
            dep = require_api_scope(s)
            assert callable(dep)
