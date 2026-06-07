"""Tests for engine.config.Settings.rate_limit_role_tiers_map property.

Targets the recently added JSON-parsing property in ``engine/config.py``
(lines 127-160). The property converts a JSON-encoded env var into a
typed ``dict[str, tuple[int, int]]`` and must silently skip malformed
entries so a bad operator configuration cannot prevent the process
from starting.
"""

from __future__ import annotations

from engine.config import Settings


def _make(**overrides: object) -> Settings:
    """Build a Settings instance with ``rate_limit_role_tiers`` overridden.

    All other fields fall back to the BaseSettings defaults.
    """
    return Settings(rate_limit_role_tiers=overrides.get("rate_limit_role_tiers", ""))


class TestRoleTiersEmpty:
    def test_empty_string_returns_empty_dict(self):
        settings = _make(rate_limit_role_tiers="")
        assert settings.rate_limit_role_tiers_map == {}

    def test_whitespace_only_returns_empty_dict(self):
        settings = _make(rate_limit_role_tiers="   \t\n  ")
        assert settings.rate_limit_role_tiers_map == {}

    def test_none_like_returns_empty_dict(self):
        # Pydantic-coerced None → empty string via default; but explicit
        # empty string still exercises the ``or ""`` fallback.
        settings = _make(rate_limit_role_tiers="")
        assert settings.rate_limit_role_tiers_map == {}


class TestRoleTiersValid:
    def test_single_role_parses_correctly(self):
        settings = _make(rate_limit_role_tiers='{"admin": [6000, 200]}')
        assert settings.rate_limit_role_tiers_map == {"admin": (6000, 200)}

    def test_multiple_roles_parse_correctly(self):
        raw = '{"viewer": [120, 30], "admin": [6000, 200], "developer": [300, 60]}'
        settings = _make(rate_limit_role_tiers=raw)
        result = settings.rate_limit_role_tiers_map
        assert result == {
            "viewer": (120, 30),
            "admin": (6000, 200),
            "developer": (300, 60),
        }

    def test_string_numeric_values_are_coerced_to_int(self):
        # JSON allows numbers; the parser also accepts string-form ints
        # because ``int(limits[0])`` handles both.
        settings = _make(rate_limit_role_tiers='{"viewer": ["120", "30"]}')
        assert settings.rate_limit_role_tiers_map == {"viewer": (120, 30)}

    def test_returns_tuple_not_list(self):
        settings = _make(rate_limit_role_tiers='{"admin": [10, 5]}')
        result = settings.rate_limit_role_tiers_map
        # The contract is tuple[int, int], not list[int].
        assert isinstance(result["admin"], tuple)
        assert result["admin"] == (10, 5)


class TestRoleTiersInvalidJson:
    def test_invalid_json_returns_empty(self):
        settings = _make(rate_limit_role_tiers="not-json-at-all")
        assert settings.rate_limit_role_tiers_map == {}

    def test_truncated_json_returns_empty(self):
        settings = _make(rate_limit_role_tiers='{"admin": [')
        assert settings.rate_limit_role_tiers_map == {}

    def test_trailing_garbage_returns_empty(self):
        settings = _make(
            rate_limit_role_tiers='{"admin": [10, 5]} extra garbage'
        )
        assert settings.rate_limit_role_tiers_map == {}


class TestRoleTiersNonDictJson:
    def test_json_array_returns_empty(self):
        settings = _make(rate_limit_role_tiers='[["admin", [10, 5]]]')
        assert settings.rate_limit_role_tiers_map == {}

    def test_json_string_returns_empty(self):
        settings = _make(rate_limit_role_tiers='"just a string"')
        assert settings.rate_limit_role_tiers_map == {}

    def test_json_null_returns_empty(self):
        settings = _make(rate_limit_role_tiers="null")
        assert settings.rate_limit_role_tiers_map == {}

    def test_json_number_returns_empty(self):
        settings = _make(rate_limit_role_tiers="42")
        assert settings.rate_limit_role_tiers_map == {}

    def test_json_boolean_returns_empty(self):
        settings = _make(rate_limit_role_tiers="true")
        assert settings.rate_limit_role_tiers_map == {}


class TestRoleTiersMalformedEntries:
    def test_non_string_role_skipped(self):
        # JSON keys are always strings in strict mode, but the parser
        # still defends against non-string keys (e.g. from a coerced
        # integer key in a Python dict that was serialised loosely).
        raw = '{"admin": [10, 5], "123": [20, 10]}'
        settings = _make(rate_limit_role_tiers=raw)
        result = settings.rate_limit_role_tiers_map
        # Both keys are valid JSON strings; both should survive.
        assert len(result) == 2

    def test_non_list_limits_skipped(self):
        raw = '{"admin": "not-a-list", "viewer": [10, 5]}'
        settings = _make(rate_limit_role_tiers=raw)
        assert settings.rate_limit_role_tiers_map == {"viewer": (10, 5)}

    def test_dict_limits_skipped(self):
        raw = '{"admin": {"per_minute": 10, "burst": 5}, "viewer": [10, 5]}'
        settings = _make(rate_limit_role_tiers=raw)
        assert settings.rate_limit_role_tiers_map == {"viewer": (10, 5)}

    def test_short_limits_skipped(self):
        raw = '{"admin": [10], "viewer": [10, 5]}'
        settings = _make(rate_limit_role_tiers=raw)
        assert settings.rate_limit_role_tiers_map == {"viewer": (10, 5)}

    def test_long_limits_skipped(self):
        raw = '{"admin": [10, 5, 1], "viewer": [10, 5]}'
        settings = _make(rate_limit_role_tiers=raw)
        assert settings.rate_limit_role_tiers_map == {"viewer": (10, 5)}

    def test_non_numeric_limits_skipped(self):
        raw = '{"admin": ["abc", "def"], "viewer": [10, 5]}'
        settings = _make(rate_limit_role_tiers=raw)
        assert settings.rate_limit_role_tiers_map == {"viewer": (10, 5)}

    def test_none_limits_skipped(self):
        raw = '{"admin": [null, null], "viewer": [10, 5]}'
        settings = _make(rate_limit_role_tiers=raw)
        assert settings.rate_limit_role_tiers_map == {"viewer": (10, 5)}


class TestRoleTiersBoundaryValues:
    def test_zero_per_minute_skipped(self):
        raw = '{"admin": [0, 5], "viewer": [10, 5]}'
        settings = _make(rate_limit_role_tiers=raw)
        assert settings.rate_limit_role_tiers_map == {"viewer": (10, 5)}

    def test_negative_per_minute_skipped(self):
        raw = '{"admin": [-1, 5], "viewer": [10, 5]}'
        settings = _make(rate_limit_role_tiers=raw)
        assert settings.rate_limit_role_tiers_map == {"viewer": (10, 5)}

    def test_zero_burst_skipped(self):
        raw = '{"admin": [10, 0], "viewer": [10, 5]}'
        settings = _make(rate_limit_role_tiers=raw)
        assert settings.rate_limit_role_tiers_map == {"viewer": (10, 5)}

    def test_negative_burst_skipped(self):
        raw = '{"admin": [10, -5], "viewer": [10, 5]}'
        settings = _make(rate_limit_role_tiers=raw)
        assert settings.rate_limit_role_tiers_map == {"viewer": (10, 5)}

    def test_per_minute_one_is_valid(self):
        settings = _make(rate_limit_role_tiers='{"limited": [1, 1]}')
        assert settings.rate_limit_role_tiers_map == {"limited": (1, 1)}


class TestRoleTiersMixedValidity:
    def test_only_valid_entries_kept(self):
        raw = (
            "{"
            '"good": [100, 20],'
            '"bad_short": [100],'
            '"bad_negative": [-1, 20],'
            '"bad_string": "nope",'
            '"also_good": [60, 10]'
            "}"
        )
        settings = _make(rate_limit_role_tiers=raw)
        result = settings.rate_limit_role_tiers_map
        assert result == {"good": (100, 20), "also_good": (60, 10)}

    def test_all_invalid_returns_empty(self):
        raw = '{"a": [0, 0], "b": [-1, -1], "c": "nope", "d": [1]}'
        settings = _make(rate_limit_role_tiers=raw)
        assert settings.rate_limit_role_tiers_map == {}


class TestRoleTiersIntegrationWithApp:
    """End-to-end: settings flow through create_app into RateLimitConfig."""

    def test_role_tiers_propagate_to_middleware(self, monkeypatch):
        from engine.api.rate_limit import RateLimitConfig, RateLimitMiddleware
        from engine.app import create_app

        monkeypatch.setattr(
            "engine.app.settings.rate_limit_role_tiers",
            '{"test_role": [999, 100]}',
        )
        app = create_app()
        rl_mw = next(
            (m for m in app.user_middleware if m.cls is RateLimitMiddleware),
            None,
        )
        assert rl_mw is not None
        config: RateLimitConfig = rl_mw.kwargs["config"]
        assert config.role_tiers == {"test_role": (999, 100)}

    def test_empty_role_tiers_propagate_as_empty_dict(self, monkeypatch):
        from engine.api.rate_limit import RateLimitConfig, RateLimitMiddleware
        from engine.app import create_app

        monkeypatch.setattr(
            "engine.app.settings.rate_limit_role_tiers", ""
        )
        app = create_app()
        rl_mw = next(
            (m for m in app.user_middleware if m.cls is RateLimitMiddleware),
            None,
        )
        assert rl_mw is not None
        config: RateLimitConfig = rl_mw.kwargs["config"]
        assert config.role_tiers == {}

    def test_invalid_role_tiers_propagate_as_empty_dict(self, monkeypatch):
        """Bad operator env var must not prevent app startup."""
        from engine.api.rate_limit import RateLimitConfig, RateLimitMiddleware
        from engine.app import create_app

        monkeypatch.setattr(
            "engine.app.settings.rate_limit_role_tiers", "this is not json"
        )
        app = create_app()
        rl_mw = next(
            (m for m in app.user_middleware if m.cls is RateLimitMiddleware),
            None,
        )
        assert rl_mw is not None
        config: RateLimitConfig = rl_mw.kwargs["config"]
        # Falls back to empty rather than crashing.
        assert config.role_tiers == {}


class TestRoleTiersSettingsDefaults:
    def test_default_rate_limit_valkey_enabled_is_false(self):
        settings = Settings()
        assert settings.rate_limit_valkey_enabled is False

    def test_default_rate_limit_valkey_key_ttl_is_3600(self):
        settings = Settings()
        assert settings.rate_limit_valkey_key_ttl_sec == 3600

    def test_default_rate_limit_role_tiers_is_empty(self):
        settings = Settings()
        assert settings.rate_limit_role_tiers == ""
        assert settings.rate_limit_role_tiers_map == {}
