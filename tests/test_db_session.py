"""Tests for engine.db.session — get_engine, get_session_factory, dispose_engine."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import engine.db.session as mod


class TestGetEngine:
    def test_returns_async_engine(self):
        mod._engine = None
        mod._session_factory = None
        engine = mod.get_engine()
        assert isinstance(engine, AsyncEngine)
        mod._engine = None

    def test_returns_same_instance_on_second_call(self):
        mod._engine = None
        mod._session_factory = None
        e1 = mod.get_engine()
        e2 = mod.get_engine()
        assert e1 is e2
        mod._engine = None


class TestGetSessionFactory:
    def test_returns_session_maker(self):
        mod._engine = None
        mod._session_factory = None
        factory = mod.get_session_factory()
        assert isinstance(factory, async_sessionmaker)
        mod._engine = None
        mod._session_factory = None

    def test_returns_same_instance_on_second_call(self):
        mod._engine = None
        mod._session_factory = None
        f1 = mod.get_session_factory()
        f2 = mod.get_session_factory()
        assert f1 is f2
        mod._engine = None
        mod._session_factory = None


class TestDisposeEngine:
    async def test_dispose_clears_globals(self):
        mod._engine = None
        mod._session_factory = None
        mod.get_engine()
        assert mod._engine is not None
        await mod.dispose_engine()
        assert mod._engine is None
        assert mod._session_factory is None

    async def test_idempotent_when_already_none(self):
        mod._engine = None
        mod._session_factory = None
        await mod.dispose_engine()
        assert mod._engine is None
