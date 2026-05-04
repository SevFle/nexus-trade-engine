"""Regression tests for User.is_active default behavior.

Bug: SQLAlchemy mapped_column(default=True) only sets the SQL INSERT default,
not the Python __init__ default. Auth providers must explicitly set
is_active=True when creating new users, otherwise the in-memory User object
has is_active=None (falsy), causing "Account is disabled" errors.

See: pyproject.toml [tool.coverage.run] for coverage source configuration.
See: engine/api/auth/*.py for the auth provider fixes.
"""

from __future__ import annotations

from engine.db.models import User


class TestUserIsActiveDefault:
    def test_user_is_active_explicitly_set(self):
        user = User(
            email="test@example.com",
            display_name="Test",
            is_active=True,
            role="user",
            auth_provider="oidc",
            external_id="ext-123",
            hashed_password=None,
        )
        assert user.is_active is True

    def test_user_without_is_active_defaults_to_none(self):
        user = User(
            email="test@example.com",
            display_name="Test",
            role="user",
            auth_provider="local",
            hashed_password="hash",
        )
        assert user.is_active is None

    def test_user_is_active_none_is_falsy(self):
        user = User(
            email="test@example.com",
            display_name="Test",
            role="user",
        )
        assert not user.is_active


class TestCoverageConfigAlignment:
    def test_engine_package_importable(self):
        import engine
        assert engine is not None

    def test_engine_api_auth_oidc_importable(self):
        from engine.api.auth.oidc import OIDCAuthProvider
        assert OIDCAuthProvider is not None

    def test_engine_config_importable(self):
        from engine.config import settings
        assert settings is not None
