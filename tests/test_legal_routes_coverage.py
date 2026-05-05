"""Tests for engine.api.routes.legal — legal document endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from engine.api.auth.dependency import get_current_user
from engine.api.routes.legal import _apply_substitutions, _escape_markdown, _strip_front_matter
from engine.app import create_app
from engine.deps import get_db
from tests.conftest import _fake_authenticated_user


class TestEscapeMarkdown:
    def test_escapes_special_chars(self):
        result = _escape_markdown("hello *world* [link](url)")
        assert "\\*" in result
        assert "\\[" in result
        assert "\\]" in result

    def test_plain_text_unchanged(self):
        result = _escape_markdown("hello world")
        assert result == "hello world"


class TestApplySubstitutions:
    def test_replaces_operator_name(self):
        result = _apply_substitutions("Welcome {{OPERATOR_NAME}}", "2024-01-01")
        assert "{{OPERATOR_NAME}}" not in result

    def test_replaces_effective_date(self):
        result = _apply_substitutions("Effective {{EFFECTIVE_DATE}}", "2024-06-01")
        assert "2024-06-01" in result

    def test_replaces_all_tokens(self):
        content = "{{OPERATOR_NAME}} {{OPERATOR_EMAIL}} {{OPERATOR_URL}} {{JURISDICTION}} {{PLATFORM_FEE_PERCENT}} {{EFFECTIVE_DATE}}"
        result = _apply_substitutions(content, "2024-01-01")
        assert "{{" not in result


class TestStripFrontMatter:
    def test_strips_yaml_front_matter(self):
        text = "---\ntitle: Test\n---\nContent here"
        result = _strip_front_matter(text)
        assert result == "Content here"

    def test_no_front_matter(self):
        text = "Just content"
        assert _strip_front_matter(text) == "Just content"

    def test_incomplete_front_matter(self):
        text = "---\ntitle: Test\nNo closing"
        assert _strip_front_matter(text) == text

    def test_empty_after_front_matter(self):
        text = "---\ntitle: Test\n---\n"
        result = _strip_front_matter(text)
        assert result == ""


class TestLegalEndpoints:
    @pytest.mark.asyncio
    async def test_list_documents(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/legal/documents")
            assert resp.status_code == 200
            data = resp.json()
            assert "documents" in data

    @pytest.mark.asyncio
    async def test_list_attributions(self, db_session):
        app = create_app()

        async def override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: _fake_authenticated_user()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/v1/legal/attributions")
            assert resp.status_code == 200
            data = resp.json()
            assert "attributions" in data
