"""Integration tests for recent auth changes.

Validates that map_roles reflects upstream IdP roles faithfully and that
require_role checks work end-to-end after SEV-741.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.auth.base import AuthResult, IAuthProvider
from engine.api.auth.dependency import get_current_user, require_role
from engine.db.models import User
from tests.conftest import FAKE_USER_ID


class _ConcreteProvider(IAuthProvider):
    @property
    def name(self) -> str:
        return "test"

    async def authenticate(self, **kwargs):
        return AuthResult()


class TestRequireRoleEnforcement:
    async def test_quant_dev_accesses_developer_resource_denied(self):
        """SEV-741: ``quant_dev`` is no longer silently promoted to
        ``developer``. A user whose upstream IdP only asserts ``quant_dev``
        must NOT be granted access to ``require_role("developer")``
        resources — that would be a silent privilege escalation."""
        app = FastAPI()

        @app.get("/dev-only")
        async def handler(user: User = Depends(require_role("developer"))):
            return {"role": user.role}

        provider = _ConcreteProvider()
        mapped = provider.map_roles(["quant_dev"])
        assert mapped == "quant_dev"

        fake_user = User(
            id=FAKE_USER_ID,
            email="qd@example.com",
            display_name="Quant Dev",
            is_active=True,
            role=mapped,
            auth_provider="local",
        )

        async def _override():
            yield fake_user

        app.dependency_overrides[get_current_user] = _override

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/dev-only")
            assert resp.status_code == 403

    async def test_viewer_remains_viewer(self):
        """SEV-741: ``viewer`` is no longer silently promoted to ``user``.
        A user whose upstream IdP only asserts ``viewer`` must NOT be
        granted access to ``require_role("user")`` resources."""
        app = FastAPI()

        @app.get("/user-only")
        async def handler(user: User = Depends(require_role("user"))):
            return {"role": user.role}

        provider = _ConcreteProvider()
        mapped = provider.map_roles(["viewer"])
        assert mapped == "viewer"

        fake_user = User(
            id=FAKE_USER_ID,
            email="viewer@example.com",
            display_name="Viewer",
            is_active=True,
            role=mapped,
            auth_provider="local",
        )

        async def _override():
            yield fake_user

        app.dependency_overrides[get_current_user] = _override

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/user-only")
            assert resp.status_code == 403
