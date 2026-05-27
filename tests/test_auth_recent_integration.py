"""Integration tests for recent auth changes.

Validates that map_roles promotions and require_role checks work end-to-end.
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
    async def test_quant_dev_accesses_developer_resource(self):
        app = FastAPI()

        @app.get("/dev-only")
        async def handler(user: User = Depends(require_role("developer"))):
            return {"role": user.role}

        provider = _ConcreteProvider()
        mapped = provider.map_roles(["quant_dev"])
        assert mapped == "developer"

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
            assert resp.status_code == 200

    async def test_viewer_promoted_to_user(self):
        app = FastAPI()

        @app.get("/user-only")
        async def handler(user: User = Depends(require_role("user"))):
            return {"role": user.role}

        provider = _ConcreteProvider()
        mapped = provider.map_roles(["viewer"])
        assert mapped == "user"

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
            assert resp.status_code == 200
