"""Unit tests for the ``require_roles(*roles)`` RBAC dependency factory.

These tests cover the *set-membership* access-control semantics that
``require_roles`` provides, which are distinct from the hierarchical
``require_role(minimum_role)`` factory also exposed by
``engine.api.auth.dependency``.

Cases exercised (one per ``test_*``):
  1. admin access granted — role is in the allow-list.
  2. viewer blocked from admin-only endpoint — role not in allow-list → 403.
  3. missing role denied — a role not in the allow-list → 403.
  4. edge cases — empty allow-list raises ``ValueError`` at registration
     time, and a multi-role allow-list admits any matching role while
     still rejecting non-members.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.auth import dependency as dependency_module
from engine.api.auth.dependency import get_current_user, require_roles
from engine.db.models import User
from tests.conftest import FAKE_USER_ID


def _make_user(role: str) -> User:
    return User(
        id=FAKE_USER_ID,
        email=f"{role}@example.com",
        display_name=f"{role} user",
        is_active=True,
        role=role,
        auth_provider="local",
    )


def _build_app(allowed_roles: tuple[str, ...]) -> FastAPI:
    """Build an isolated FastAPI app whose sole route is guarded by
    ``require_roles(*allowed_roles)``. Auth is short-circuited via
    dependency override so the test exercises *only* the RBAC check."""
    app = FastAPI()

    @app.get("/restricted")
    async def handler(user: User = Depends(require_roles(*allowed_roles))):
        return {"role": user.role}

    return app


async def _get_with_role(app: FastAPI, role: str) -> int:
    fake_user = _make_user(role)

    async def _override():
        yield fake_user

    app.dependency_overrides[get_current_user] = _override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/restricted")
    return resp.status_code


class TestRequireRolesAdminGranted:
    """Case 1: a principal whose role is in the allow-list is admitted."""

    async def test_admin_access_granted(self):
        app = _build_app(("admin",))
        assert await _get_with_role(app, "admin") == 200

    async def test_admin_in_multi_role_allow_list(self):
        app = _build_app(("viewer", "developer", "admin"))
        assert await _get_with_role(app, "admin") == 200


class TestRequireRolesViewerBlocked:
    """Case 2: a viewer is denied on an admin-only endpoint (403)."""

    async def test_viewer_blocked_from_admin_endpoint(self):
        app = _build_app(("admin",))
        status_code = await _get_with_role(app, "viewer")
        assert status_code == 403

    async def test_blocked_response_body_is_descriptive(self):
        """The 403 body should explain which roles were required so the
        caller (or operator) can diagnose the denial."""
        app = _build_app(("admin", "developer"))

        fake_user = _make_user("viewer")

        async def _override():
            yield fake_user

        app.dependency_overrides[get_current_user] = _override

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/restricted")

        assert resp.status_code == 403
        detail = resp.json()["detail"].lower()
        # Mentions the offending role and the required set.
        assert "viewer" in detail
        assert "admin" in detail
        assert "developer" in detail


class TestRequireRolesMissingRoleDenied:
    """Case 3: a role not present in the allow-list is denied even when
    the allow-list contains multiple entries."""

    async def test_role_not_in_allow_list_denied(self):
        # quant_dev is a real role in ROLE_HIERARCHY but not in this set.
        app = _build_app(("admin", "developer", "portfolio_manager"))
        assert await _get_with_role(app, "quant_dev") == 403

    async def test_unknown_role_string_denied(self):
        """A user carrying a role string that isn't in the allow-list
        (and isn't even a known role) is still rejected — set membership
        is the only criterion, role hierarchy is not consulted."""
        app = _build_app(("admin",))
        assert await _get_with_role(app, "superuser") == 403


class TestRequireRolesEdgeCases:
    """Case 4: edge cases — empty allow-list misconfiguration and the
    no-hierarchy property of the membership check."""

    def test_empty_allow_list_raises_at_registration(self):
        """An empty allow-list would silently lock the endpoint for *every*
        principal (including admins). The factory must fail fast at
        registration time rather than producing a dependency that always
        returns 403."""
        with pytest.raises(ValueError, match="at least one role"):
            require_roles()

    def test_factory_returns_callable(self):
        dep = require_roles("admin", "developer")
        assert callable(dep)

    async def test_higher_privilege_role_not_implicitly_admitted(self):
        """Set-membership semantics: ``admin`` (the most privileged role in
        ROLE_HIERARCHY) is *not* implicitly admitted to an endpoint locked
        to ``developer`` — only roles literally listed are permitted. This
        is the key behavioral difference vs. ``require_role``."""
        app = _build_app(("developer",))
        assert await _get_with_role(app, "admin") == 403


class TestRequireRolesAuditLog:
    """The denial path must emit an audit-log record carrying the request
    path/method and the offending role, and that log call must never break
    the authz decision (a logging failure still surfaces a 403)."""

    async def test_denial_emits_audit_log_with_path_and_method(self):
        app = _build_app(("admin",))

        fake_user = _make_user("viewer")

        async def _override():
            yield fake_user

        app.dependency_overrides[get_current_user] = _override

        with patch.object(
            dependency_module.logger, "warning", return_value=None
        ) as mock_warn:
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as ac:
                resp = await ac.get("/restricted")

        assert resp.status_code == 403
        mock_warn.assert_called_once()
        kwargs = mock_warn.call_args.kwargs
        # Event name is the first positional arg.
        assert mock_warn.call_args.args == ("rbac.deny",)
        assert kwargs["role"] == "viewer"
        assert kwargs["allowed"] == ["admin"]
        # Path/method are captured from the request for the audit trail.
        assert kwargs["path"] == "/restricted"
        assert kwargs["method"] == "GET"
        assert "viewer" in kwargs["detail"]
        assert "admin" in kwargs["detail"]

    async def test_sorted_allowed_is_cached_at_registration_time(self):
        """``sorted(allowed)`` should be computed once when the factory is
        called, not on every denial. Verify the closure exposes a
        registration-time cached list rather than recomputing per call."""
        dep = require_roles("developer", "admin", "viewer")
        # The cached sorted list lives in the closure namespace.
        assert dep.__code__.co_freevars
        closure_values = {
            name: cell.cell_contents
            for name, cell in zip(
                dep.__code__.co_freevars, dep.__closure__, strict=True
            )
        }
        assert closure_values["allowed_sorted"] == [
            "admin",
            "developer",
            "viewer",
        ]
        # And the set form is preserved for membership lookup.
        assert closure_values["allowed"] == {"admin", "developer", "viewer"}

    async def test_logger_failure_does_not_mask_403(self):
        """If structlog raises inside the audit-log call, the dependency
        must still raise the original 403 — logging must never change the
        authz outcome."""
        app = _build_app(("admin",))

        fake_user = _make_user("viewer")

        async def _override():
            yield fake_user

        app.dependency_overrides[get_current_user] = _override

        with patch.object(
            dependency_module.logger, "warning", side_effect=RuntimeError("boom")
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as ac:
                resp = await ac.get("/restricted")

        assert resp.status_code == 403
        assert resp.json()["detail"].startswith("Role 'viewer' is not permitted")
