"""Targeted tests for the most-recently-changed production code.

The commit ``fix(auth): resolve 403 error on developer resource access``
introduced a ``_ROLE_PROMOTIONS`` mapping in ``engine/api/auth/base.py``
that lifts ``viewer``→``user`` and ``quant_dev``→``developer`` *after*
``map_roles`` has selected the highest-priority role.  These tests pin
that behaviour (and the surrounding contract of ``map_roles``) so future
refactors cannot silently regress it.
"""
from __future__ import annotations

import pytest

from engine.api.auth.base import _ROLE_PROMOTIONS, IAuthProvider, UserInfo


class _DummyProvider(IAuthProvider):
    """Minimal concrete provider so we can exercise ``map_roles`` directly."""

    @property
    def name(self) -> str:
        return "dummy"

    async def authenticate(self, **kwargs):  # pragma: no cover - unused here
        from engine.api.auth.base import AuthResult

        return AuthResult(success=False, error="not implemented")


@pytest.fixture
def provider() -> _DummyProvider:
    return _DummyProvider()


class TestRolePromotionMapping:
    """Direct tests against the ``_ROLE_PROMOTIONS`` table itself."""

    def test_promotion_table_is_exactly_the_two_known_entries(self):
        assert _ROLE_PROMOTIONS == {
            "viewer": "user",
            "quant_dev": "developer",
        }

    def test_promotion_targets_are_valid_application_roles(self):
        valid_roles = {
            "viewer",
            "user",
            "retail_trader",
            "quant_dev",
            "developer",
            "portfolio_manager",
            "admin",
        }
        for src, dst in _ROLE_PROMOTIONS.items():
            assert src in valid_roles, f"unknown source role: {src}"
            assert dst in valid_roles, f"unknown target role: {dst}"
            assert src != dst, "promotion must change the role"


class TestMapRolesPromotion:
    """End-to-end checks through ``IAuthProvider.map_roles``."""

    def test_quant_dev_promoted_to_developer(self, provider):
        assert provider.map_roles(["quant_dev"]) == "developer"

    def test_viewer_promoted_to_user(self, provider):
        assert provider.map_roles(["viewer"]) == "user"

    def test_quant_dev_beats_retail_trader_and_is_promoted(self, provider):
        # priority: quant_dev(3) > retail_trader(2) → quant_dev → developer
        assert provider.map_roles(["retail_trader", "quant_dev"]) == "developer"

    def test_developer_unchanged_when_directly_supplied(self, provider):
        assert provider.map_roles(["developer"]) == "developer"

    def test_user_unchanged_when_directly_supplied(self, provider):
        assert provider.map_roles(["user"]) == "user"

    def test_admin_wins_over_promotable_roles(self, provider):
        assert provider.map_roles(["viewer", "quant_dev", "admin"]) == "admin"

    def test_portfolio_manager_wins_over_quant_dev(self, provider):
        # portfolio_manager(5) > quant_dev(3); PM is *not* in the promotion
        # table so it must be returned as-is.
        assert provider.map_roles(["quant_dev", "portfolio_manager"]) == "portfolio_manager"

    def test_empty_roles_defaults_to_user(self, provider):
        assert provider.map_roles([]) == "user"

    def test_only_unknown_roles_defaults_to_user(self, provider):
        assert provider.map_roles(["superuser", "root"]) == "user"

    def test_case_insensitive_promotion(self, provider):
        assert provider.map_roles(["QUANT_DEV"]) == "developer"
        assert provider.map_roles(["Viewer"]) == "user"

    def test_whitespace_tolerant_promotion(self, provider):
        assert provider.map_roles(["  quant_dev  "]) == "developer"
        assert provider.map_roles(["\tviewer\n"]) == "user"

    def test_unknown_roles_do_not_block_promotion(self, provider):
        # Mix a known promotable role with garbage; garbage is ignored,
        # quant_dev still wins, then promotion kicks in.
        assert provider.map_roles(["garbage", "quant_dev", "?role?"]) == "developer"


class TestUserInfoDefaults:
    """Sanity-check the dataclass contract relied on by ``map_roles`` callers."""

    def test_default_roles_is_user(self):
        info = UserInfo()
        assert info.roles == ["user"]

    def test_default_roles_instances_are_independent(self):
        a = UserInfo()
        b = UserInfo()
        a.roles.append("admin")
        assert b.roles == ["user"], "default factory must not be shared"
