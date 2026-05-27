"""Tests for expanded RBAC role hierarchy (SEV-233 / gh#86).

Validates the domain-specific roles quant_dev, retail_trader,
portfolio_manager alongside the pre-existing user/developer/admin
roles, and the require_auth convenience dependency.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import (
    ROLE_HIERARCHY,
    _resolve_token,
    require_auth,
    require_role,
)
from engine.db.models import User
from tests.conftest import FAKE_USER_ID


class TestExpandedRoleHierarchy:
    def test_viewer_is_lowest(self):
        assert ROLE_HIERARCHY["viewer"] == 0

    def test_user_above_viewer(self):
        assert ROLE_HIERARCHY["user"] > ROLE_HIERARCHY["viewer"]

    def test_retail_trader_above_user(self):
        assert ROLE_HIERARCHY["retail_trader"] > ROLE_HIERARCHY["user"]

    def test_quant_dev_above_retail_trader(self):
        assert ROLE_HIERARCHY["quant_dev"] > ROLE_HIERARCHY["retail_trader"]

    def test_developer_above_quant_dev(self):
        assert ROLE_HIERARCHY["developer"] > ROLE_HIERARCHY["quant_dev"]

    def test_portfolio_manager_above_developer(self):
        assert ROLE_HIERARCHY["portfolio_manager"] > ROLE_HIERARCHY["developer"]

    def test_admin_is_highest(self):
        assert ROLE_HIERARCHY["admin"] > ROLE_HIERARCHY["portfolio_manager"]

    def test_all_roles_present(self):
        expected = {"viewer", "user", "retail_trader", "quant_dev", "developer", "portfolio_manager", "admin"}
        assert set(ROLE_HIERARCHY.keys()) == expected

    def test_backward_compatible_user_developer_admin(self):
        assert ROLE_HIERARCHY["user"] < ROLE_HIERARCHY["developer"]
        assert ROLE_HIERARCHY["developer"] < ROLE_HIERARCHY["admin"]

    def test_total_role_count(self):
        assert len(ROLE_HIERARCHY) == 7


class TestRequireRoleExpanded:
    @pytest.mark.parametrize(
        ("role", "minimum", "allowed"),
        [
            ("viewer", "viewer", True),
            ("user", "viewer", True),
            ("retail_trader", "user", True),
            ("quant_dev", "retail_trader", True),
            ("developer", "quant_dev", True),
            ("portfolio_manager", "developer", True),
            ("admin", "portfolio_manager", True),
            ("admin", "admin", True),
            ("viewer", "user", False),
            ("user", "retail_trader", False),
            ("retail_trader", "quant_dev", False),
            ("quant_dev", "developer", False),
            ("developer", "portfolio_manager", False),
            ("portfolio_manager", "admin", False),
        ],
    )
    async def test_role_access_matrix(self, role, minimum, allowed):
        app = FastAPI()

        @app.get("/test")
        async def handler(user: User = Depends(require_role(minimum))):
            return {"role": user.role}

        fake_user = User(
            id=FAKE_USER_ID,
            email="test@example.com",
            display_name="Test",
            is_active=True,
            role=role,
            auth_provider="local",
        )

        async def _override():
            yield fake_user

        from engine.api.auth.dependency import get_current_user

        app.dependency_overrides[get_current_user] = _override

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/test")

        if allowed:
            assert resp.status_code == 200
        else:
            assert resp.status_code == 403


class TestRequireAuthDependency:
    async def test_require_auth_returns_user_on_valid_jwt(self):
        app = FastAPI()

        @app.get("/protected")
        async def handler(user: User = Depends(require_auth)):
            return {"id": str(user.id), "email": user.email}

        fake_user = User(
            id=FAKE_USER_ID,
            email="auth-test@example.com",
            display_name="Auth Test",
            is_active=True,
            role="user",
            auth_provider="local",
        )

        async def _override():
            yield fake_user

        app.dependency_overrides[require_auth] = _override

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/protected")
            assert resp.status_code == 200
            assert resp.json()["email"] == "auth-test@example.com"


class TestResolveToken:
    def test_bearer_credentials(self):
        from fastapi.security import HTTPAuthorizationCredentials

        class _FakeRequest:
            headers: ClassVar[dict[str, str]] = {}

        req = _FakeRequest()
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="tok123")
        assert _resolve_token(req, creds) == "tok123"

    def test_api_key_header(self):
        class _FakeRequest:
            headers: ClassVar[dict[str, str]] = {"x-api-key": "nxs_live_abc123"}

        req = _FakeRequest()
        assert _resolve_token(req, None) == "nxs_live_abc123"

    def test_no_token_returns_none(self):
        class _FakeRequest:
            headers: ClassVar[dict[str, str]] = {}

        req = _FakeRequest()
        assert _resolve_token(req, None) is None

    def test_bearer_takes_precedence_over_api_key(self):
        from fastapi.security import HTTPAuthorizationCredentials

        class _FakeRequest:
            headers: ClassVar[dict[str, str]] = {"x-api-key": "nxs_live_abc123"}

        req = _FakeRequest()
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="jwt-token")
        assert _resolve_token(req, creds) == "jwt-token"

    def test_empty_credentials_returns_none(self):
        from fastapi.security import HTTPAuthorizationCredentials

        class _FakeRequest:
            headers: ClassVar[dict[str, str]] = {}

        req = _FakeRequest()
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="")
        assert _resolve_token(req, creds) is None


class TestBaseProviderMapRoles:
    def _make_provider(self):
        from engine.api.auth.base import AuthResult, IAuthProvider

        class _Concrete(IAuthProvider):
            @property
            def name(self):
                return "test"

            async def authenticate(self, **kwargs):
                return AuthResult()

        return _Concrete()

    def test_map_roles_admin_wins(self):
        p = self._make_provider()
        assert p.map_roles(["user", "admin", "developer"]) == "admin"

    def test_map_roles_unknown_roles_ignored(self):
        p = self._make_provider()
        assert p.map_roles(["superuser", "god"]) == "user"

    def test_map_roles_new_domain_roles(self):
        p = self._make_provider()
        assert p.map_roles(["retail_trader", "quant_dev"]) == "developer"
        assert p.map_roles(["portfolio_manager", "quant_dev"]) == "portfolio_manager"
        assert p.map_roles(["viewer"]) == "user"
        assert p.map_roles(["retail_trader"]) == "retail_trader"
        assert p.map_roles(["portfolio_manager"]) == "portfolio_manager"

    def test_map_roles_empty_list_returns_user(self):
        p = self._make_provider()
        assert p.map_roles([]) == "user"


class TestAuthExports:
    def test_require_auth_importable(self):
        from engine.api.auth import require_auth

        assert callable(require_auth)

    def test_all_exports_present(self):
        import engine.api.auth as auth_mod

        for name in auth_mod.__all__:
            assert hasattr(auth_mod, name), f"Missing export: {name}"
