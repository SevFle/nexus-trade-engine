"""Tests for engine.api.auth.dependency — auth dependency edge cases.

Covers _user_from_jwt, _load_active_user, and require_api_scope
integration paths that the e2e tests don't exercise.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import get_current_user, require_api_scope
from engine.api.auth.jwt import create_access_token
from engine.config import settings
from engine.db.models import User
from engine.deps import get_db


@pytest.fixture(autouse=True)
def _set_test_secret_key():
    """These tests mint and verify real JWTs via ``create_access_token`` /
    ``get_current_user``, which require a non-empty HMAC ``secret_key``.
    ``settings.secret_key`` defaults to "" (other suites rely on that), so set
    it for the duration of each test and restore it. Without this the tests
    fail with ``jwt.exceptions.InvalidKeyError: HMAC key must not be empty``
    unless another auth suite happens to have set it first."""
    original = settings.secret_key
    settings.secret_key = "test-secret-key-for-dependency-tests"
    yield
    settings.secret_key = original


class TestUserFromJwt:
    async def test_expired_token_returns_401(self, db_session):
        app = FastAPI()
        expired_token = create_access_token(
            sub=str(uuid.uuid4()),
            email="expired@example.com",
            role="user",
            expires_delta=timedelta(seconds=-1),
        )

        @app.get("/test")
        async def handler(user: User = Depends(get_current_user)):
            return {"ok": True}

        async def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(
                "/test", headers={"Authorization": f"Bearer {expired_token}"}
            )
            assert resp.status_code == 401

    async def test_nonexistent_user_returns_401(self, db_session):
        app = FastAPI()
        fake_token = create_access_token(
            sub=str(uuid.uuid4()),
            email="ghost@example.com",
            role="user",
        )

        @app.get("/test")
        async def handler(user: User = Depends(get_current_user)):
            return {"ok": True}

        async def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(
                "/test", headers={"Authorization": f"Bearer {fake_token}"}
            )
            assert resp.status_code == 401
            assert "not found" in resp.json()["detail"].lower()

    async def test_inactive_user_returns_401(self, db_session):
        app = FastAPI()

        user = User(
            email="inactive-dep@example.com",
            hashed_password="x",
            display_name="Inactive",
            is_active=False,
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        token = create_access_token(
            sub=str(user.id), email=user.email, role=user.role
        )

        @app.get("/test")
        async def handler(user: User = Depends(get_current_user)):
            return {"ok": True}

        async def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(
                "/test", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 401
            assert "disabled" in resp.json()["detail"].lower()

    async def test_valid_user_returns_200(self, db_session):
        app = FastAPI()

        user = User(
            email="valid-dep@example.com",
            hashed_password="x",
            display_name="Valid",
            is_active=True,
            role="quant_dev",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        token = create_access_token(
            sub=str(user.id), email=user.email, role=user.role
        )

        @app.get("/test")
        async def handler(user: User = Depends(get_current_user)):
            return {"role": user.role}

        async def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(
                "/test", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 200
            assert resp.json()["role"] == "quant_dev"


class TestRequireApiScopeIntegration:
    async def test_jwt_user_bypasses_scope_check(self, db_session):
        app = FastAPI()

        user = User(
            email="jwt-scope@example.com",
            hashed_password="x",
            display_name="JWT",
            is_active=True,
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        token = create_access_token(
            sub=str(user.id), email=user.email, role=user.role
        )

        @app.get("/test")
        async def handler(user: User = Depends(require_api_scope("admin"))):
            return {"ok": True}

        async def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(
                "/test", headers={"Authorization": f"Bearer {token}"}
            )
            assert resp.status_code == 200
