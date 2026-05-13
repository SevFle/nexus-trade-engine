"""Tests for engine.api.auth.local — LocalAuthProvider authentication.

Improves coverage for password hashing, user creation, and error paths.
"""

from __future__ import annotations

import asyncio

import pytest

from engine.api.auth.base import UserInfo
from engine.api.auth.local import LocalAuthProvider, _hash_password, _verify_password
from engine.db.models import User


class TestPasswordHashing:
    def test_hash_and_verify_roundtrip(self):
        pw = "correcthorsebatterystaple"
        hashed = _hash_password(pw)
        assert hashed != pw
        assert _verify_password(pw, hashed)

    def test_wrong_password_fails(self):
        hashed = _hash_password("password123")
        assert not _verify_password("wrong", hashed)

    def test_hash_is_bcrypt_format(self):
        hashed = _hash_password("test")
        assert hashed.startswith("$2")

    def test_different_hashes_for_same_password(self):
        pw = "same_password"
        h1 = _hash_password(pw)
        h2 = _hash_password(pw)
        assert h1 != h2


@pytest.fixture
def provider():
    return LocalAuthProvider()


class TestLocalAuthProviderProperties:
    def test_name(self, provider):
        assert provider.name == "local"

    def test_authenticate_missing_email(self, provider):
        result = asyncio.get_event_loop().run_until_complete(
            provider.authenticate(password="pw", db=None)
        )
        assert not result.success
        assert "required" in result.error.lower()

    def test_authenticate_missing_password(self, provider):
        result = asyncio.get_event_loop().run_until_complete(
            provider.authenticate(email="a@b.com", db=None)
        )
        assert not result.success

    def test_authenticate_missing_db(self, provider):
        result = asyncio.get_event_loop().run_until_complete(
            provider.authenticate(email="a@b.com", password="pw")
        )
        assert not result.success


class TestLocalAuthProviderAuthenticate:
    async def test_authenticate_unknown_email(self, provider, db_session):
        result = await provider.authenticate(
            email="nonexistent@example.com", password="whatever123456", db=db_session
        )
        assert not result.success
        assert "invalid credentials" in result.error.lower()

    async def test_authenticate_wrong_password(self, provider, db_session):
        hashed = _hash_password("correctpassword")
        user = User(
            email="auth-test@example.com",
            hashed_password=hashed,
            display_name="Auth Test",
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        result = await provider.authenticate(
            email="auth-test@example.com", password="wrongpassword", db=db_session
        )
        assert not result.success

    async def test_authenticate_success(self, provider, db_session):
        hashed = _hash_password("mypassword123")
        user = User(
            email="login-success@example.com",
            hashed_password=hashed,
            display_name="Login User",
            role="retail_trader",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        result = await provider.authenticate(
            email="login-success@example.com", password="mypassword123", db=db_session
        )
        assert result.success
        assert result.user_info is not None
        assert result.user_info.email == "login-success@example.com"
        assert "retail_trader" in result.user_info.roles

    async def test_authenticate_non_local_provider_rejected(self, provider, db_session):
        user = User(
            email="oauth-user@example.com",
            hashed_password=None,
            display_name="OAuth User",
            role="user",
            auth_provider="google",
        )
        db_session.add(user)
        await db_session.flush()

        result = await provider.authenticate(
            email="oauth-user@example.com", password="anything", db=db_session
        )
        assert not result.success

    async def test_authenticate_inactive_user(self, provider, db_session):
        hashed = _hash_password("testpassword")
        user = User(
            email="inactive@example.com",
            hashed_password=hashed,
            display_name="Inactive",
            role="user",
            auth_provider="local",
            is_active=False,
        )
        db_session.add(user)
        await db_session.flush()

        result = await provider.authenticate(
            email="inactive@example.com", password="testpassword", db=db_session
        )
        assert not result.success
        assert "disabled" in result.error.lower()


class TestLocalAuthProviderCreateUser:
    async def test_create_user_success(self, provider, db_session):
        user_info = UserInfo(email="newuser@example.com", display_name="New User")
        result = await provider.create_user(
            user_info=user_info, password="strongpassword123", db=db_session
        )
        assert result.success
        assert result.user_info is not None
        assert result.user_info.email == "newuser@example.com"

    async def test_create_user_missing_db(self, provider):
        user_info = UserInfo(email="newuser@example.com")
        result = await provider.create_user(
            user_info=user_info, password="strongpassword123"
        )
        assert not result.success

    async def test_create_user_missing_password(self, provider, db_session):
        user_info = UserInfo(email="newuser@example.com")
        result = await provider.create_user(
            user_info=user_info, db=db_session
        )
        assert not result.success

    async def test_create_user_short_password(self, provider, db_session):
        user_info = UserInfo(email="short@example.com", display_name="Short")
        result = await provider.create_user(
            user_info=user_info, password="short", db=db_session
        )
        assert not result.success
        assert "8" in result.error

    async def test_create_user_duplicate_email(self, provider, db_session):
        hashed = _hash_password("existingpass")
        user = User(
            email="dup@example.com",
            hashed_password=hashed,
            display_name="Dup",
            role="user",
            auth_provider="local",
        )
        db_session.add(user)
        await db_session.flush()

        user_info = UserInfo(email="dup@example.com", display_name="Dup2")
        result = await provider.create_user(
            user_info=user_info, password="anotherpassword", db=db_session
        )
        assert not result.success
        assert "already" in result.error.lower()
