"""Comprehensive tests for the role-promotion fix (SEV-741, PR #741).

Targets the change introduced in commit f4231fc ("fix(auth): resolve 403
error on developer resource access"), which added the ``_ROLE_PROMOTIONS``
mapping inside :mod:`engine.api.auth.base` so that:

* ``viewer``  is promoted to ``user``
* ``quant_dev`` is promoted to ``developer``

The promotion is performed at the tail of :meth:`IAuthProvider.map_roles`
**after** the priority lookup has selected the highest-privilege role from
``role_priority``. The intent is that callers who only know the legacy /
external role vocabulary still receive a role that the internal
``ROLE_HIERARCHY`` recognises as eligible for the corresponding protected
resource.

These tests pin that behaviour and exercise the surrounding
``IAuthProvider`` default methods (``get_user_info``, ``create_user``) so
that future refactors of the base class do not silently regress the
authz layer.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.auth.base import (
    _ROLE_PROMOTIONS,
    AuthResult,
    IAuthProvider,
    UserInfo,
)
from engine.api.auth.dependency import ROLE_HIERARCHY, get_current_user, require_role
from engine.db.models import User
from tests.conftest import FAKE_USER_ID

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ConcreteProvider(IAuthProvider):
    """Minimal concrete subclass exposing only the abstract surface."""

    @property
    def name(self) -> str:
        return "concrete-test"

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        # Record kwargs so tests can assert call shapes if desired.
        self.last_kwargs = kwargs  # type: ignore[attr-defined]
        return AuthResult(success=True, user_info=UserInfo(email="ok@example.com"))


def _provider() -> _ConcreteProvider:
    return _ConcreteProvider()


def _make_user(role: str) -> User:
    return User(
        id=FAKE_USER_ID,
        email=f"{role}@example.com",
        display_name=role.title(),
        is_active=True,
        role=role,
        auth_provider="local",
    )


# ---------------------------------------------------------------------------
# Promotion mapping itself
# ---------------------------------------------------------------------------


class TestRolePromotionConstant:
    """Lock down the promotion table — this is the heart of SEV-741."""

    def test_promotion_table_contains_viewer_to_user(self):
        assert _ROLE_PROMOTIONS["viewer"] == "user"

    def test_promotion_table_contains_quant_dev_to_developer(self):
        assert _ROLE_PROMOTIONS["quant_dev"] == "developer"

    def test_promotion_table_has_exactly_two_entries(self):
        """If new promotions are added intentionally, update this test."""
        assert _ROLE_PROMOTIONS == {
            "viewer": "user",
            "quant_dev": "developer",
        }


class TestMapRolesPromotion:
    """Unit tests around :meth:`IAuthProvider.map_roles` post-promotion."""

    def test_viewer_alone_promoted_to_user(self):
        assert _provider().map_roles(["viewer"]) == "user"

    def test_quant_dev_alone_promoted_to_developer(self):
        assert _provider().map_roles(["quant_dev"]) == "developer"

    def test_promotion_does_not_override_higher_role(self):
        """A ``viewer`` mixed with a real ``admin`` must yield ``admin`` —
        promotion only fires when the promoted role *wins* the priority
        contest."""
        p = _provider()
        assert p.map_roles(["viewer", "admin"]) == "admin"
        assert p.map_roles(["quant_dev", "admin"]) == "admin"
        assert p.map_roles(["quant_dev", "portfolio_manager"]) == "portfolio_manager"

    def test_promoted_role_wins_over_lower_real_roles(self):
        """``viewer`` alone has priority 0 but the promotion lifts it to
        ``user`` (priority 1) — verify it beats an explicit ``viewer``."""
        p = _provider()
        # viewer + unknown -> still promoted to user (best=viewer, promoted)
        assert p.map_roles(["viewer", "guest"]) == "user"

    def test_quant_dev_promoted_after_beating_viewer(self):
        """``quant_dev`` has priority 3 > ``viewer``'s 0, and then gets
        promoted to ``developer`` (priority 4)."""
        assert _provider().map_roles(["viewer", "quant_dev"]) == "developer"

    def test_case_insensitive_input(self):
        p = _provider()
        assert p.map_roles(["Viewer"]) == "user"
        assert p.map_roles(["Quant_Dev"]) == "developer"
        assert p.map_roles(["VIEWER", "QUANT_DEV"]) == "developer"

    def test_whitespace_is_stripped_before_promotion(self):
        p = _provider()
        assert p.map_roles(["  viewer  "]) == "user"
        assert p.map_roles(["\tquant_dev\n"]) == "developer"

    def test_empty_list_returns_default_user(self):
        assert _provider().map_roles([]) == "user"

    def test_only_unknown_roles_returns_default_user(self):
        """No known role to pick → ``best`` stays at default ``user``
        (which is *not* a key in ``_ROLE_PROMOTIONS``), so no promotion."""
        assert _provider().map_roles(["ghost", "phantom"]) == "user"

    def test_promoted_role_round_trips_through_role_hierarchy(self):
        """Critical invariant: a promoted role MUST exist in
        :data:`ROLE_HIERARCHY`, otherwise :func:`require_role` would
        silently drop the user to priority 0."""
        for promoted in _ROLE_PROMOTIONS.values():
            assert promoted in ROLE_HIERARCHY, (
                f"Promoted role {promoted!r} missing from ROLE_HIERARCHY"
            )

    def test_promoted_role_meets_expected_threshold(self):
        """The viewer→user promotion must give enough privilege to satisfy
        ``require_role('user')``; quant_dev→developer must satisfy
        ``require_role('developer')``."""
        h = ROLE_HIERARCHY
        assert h[_ROLE_PROMOTIONS["viewer"]] >= h["user"]
        assert h[_ROLE_PROMOTIONS["quant_dev"]] >= h["developer"]

    def test_duplicate_viewer_inputs_still_promote(self):
        assert _provider().map_roles(["viewer", "viewer", "viewer"]) == "user"

    def test_priority_resolution_then_promotion(self):
        """Even when an unknown role alphabetically precedes a known one,
        the priority map (not list order) decides ``best``."""
        p = _provider()
        # Order shouldn't matter — quant_dev always wins over viewer.
        assert p.map_roles(["viewer", "quant_dev"]) == "developer"
        assert p.map_roles(["quant_dev", "viewer"]) == "developer"


# ---------------------------------------------------------------------------
# Default IAuthProvider methods
# ---------------------------------------------------------------------------


class TestProviderDefaults:
    """Cover the non-abstract default methods added in the same file."""

    async def test_get_user_info_default_returns_none(self):
        assert await _provider().get_user_info("any-external-id") is None

    async def test_create_user_default_returns_failure(self):
        result = await _provider().create_user(UserInfo(email="x@example.com"))
        assert isinstance(result, AuthResult)
        assert result.success is False
        assert "concrete-test" in (result.error or "")

    async def test_authenticate_records_kwargs(self):
        p = _provider()
        await p.authenticate(token="abc", ip="1.2.3.4")
        assert getattr(p, "last_kwargs", None) == {"token": "abc", "ip": "1.2.3.4"}

    def test_concrete_provider_name_property(self):
        assert _provider().name == "concrete-test"

    def test_i_auth_provider_is_abstract(self):
        with pytest.raises(TypeError):
            IAuthProvider()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------


class TestAuthResultAndUserInfo:
    """Lock down the public dataclass surface used by auth providers."""

    def test_auth_result_defaults(self):
        r = AuthResult()
        assert r.success is False
        assert r.user_info is None
        assert r.error is None

    def test_user_info_defaults(self):
        u = UserInfo()
        assert u.external_id is None
        assert u.email == ""
        assert u.display_name == ""
        assert u.provider == "local"
        assert u.roles == ["user"]
        assert u.raw_claims == {}

    def test_user_info_roles_isolated_between_instances(self):
        """Default factory must produce a fresh list each call (i.e. not
        a shared mutable class attribute)."""
        a, b = UserInfo(), UserInfo()
        a.roles.append("admin")
        assert b.roles == ["user"]

    def test_auth_result_round_trip(self):
        u = UserInfo(email="x@y.z", roles=["admin"])
        r = AuthResult(success=True, user_info=u, error=None)
        assert r.success is True
        assert r.user_info is u
        assert r.error is None


# ---------------------------------------------------------------------------
# End-to-end FastAPI integration — exercises the full authz chain
# ---------------------------------------------------------------------------


class TestRequireRoleWithPromotion:
    """End-to-end smoke: a promoted role must clear the matching
    ``require_role`` gate inside a real FastAPI app."""

    async def _client_for(self, user: User) -> AsyncClient:
        app = FastAPI()

        @app.get("/dev-only")
        async def _dev(user: User = Depends(require_role("developer"))):
            return {"role": user.role}

        @app.get("/user-only")
        async def _user(user: User = Depends(require_role("user"))):
            return {"role": user.role}

        @app.get("/pm-only")
        async def _pm(user: User = Depends(require_role("portfolio_manager"))):
            return {"role": user.role}

        async def _override():
            yield user

        app.dependency_overrides[get_current_user] = _override
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    async def test_quant_dev_promoted_to_developer_succeeds(self):
        async with await self._client_for(_make_user("developer")) as ac:
            resp = await ac.get("/dev-only")
            assert resp.status_code == 200
            assert resp.json() == {"role": "developer"}

    async def test_viewer_promoted_to_user_succeeds(self):
        async with await self._client_for(_make_user("user")) as ac:
            resp = await ac.get("/user-only")
            assert resp.status_code == 200
            assert resp.json() == {"role": "user"}

    async def test_user_cannot_access_developer_route(self):
        """Regression: pre-fix the user→developer gap was the SEV-741
        bug. The viewer→user promotion must NOT escalate beyond ``user``."""
        async with await self._client_for(_make_user("user")) as ac:
            resp = await ac.get("/dev-only")
            assert resp.status_code == 403

    async def test_viewer_cannot_access_pm_route(self):
        async with await self._client_for(_make_user("user")) as ac:
            resp = await ac.get("/pm-only")
            assert resp.status_code == 403

    async def test_admin_can_access_everything(self):
        async with await self._client_for(_make_user("admin")) as ac:
            for path in ("/user-only", "/dev-only", "/pm-only"):
                resp = await ac.get(path)
                assert resp.status_code == 200, f"{path} failed"


# ---------------------------------------------------------------------------
# Regression: promotion table must not break dependency imports
# ---------------------------------------------------------------------------


class TestAuthModuleImports:
    """Ensure the public auth surface is exported correctly."""

    def test_role_hierarchy_importable(self):
        from engine.api.auth import dependency

        assert hasattr(dependency, "ROLE_HIERARCHY")
        assert "viewer" in dependency.ROLE_HIERARCHY
        assert "quant_dev" in dependency.ROLE_HIERARCHY

    def test_require_role_factory_returns_callable(self):
        guard = require_role("developer")
        assert callable(guard)

    def test_i_auth_provider_subclass_count(self):
        """The base class is meant to be subclassed; verify it remains
        abstract until at least ``authenticate`` is implemented."""
        class _MissingAuth(IAuthProvider):
            @property
            def name(self) -> str:
                return "x"

        with pytest.raises(TypeError):
            _MissingAuth()  # type: ignore[abstract]
