"""Integration tests for recent auth changes.

Validates that map_roles reflects upstream IdP roles faithfully and that
require_role checks work end-to-end after SEV-741.

The tests in this module pin the post-SEV-741 contract:

* ``IAuthProvider.map_roles`` performs **no implicit promotion** of
  upstream IdP roles. A user whose IdP only asserts ``quant_dev`` is
  persisted with ``role="quant_dev"`` — never silently escalated to
  ``developer``.
* :func:`engine.api.auth.dependency.require_role` then enforces the
  internal role hierarchy strictly: a ``quant_dev`` user (level 3) is
  denied access to a resource guarded by ``require_role("developer")``
  (level 4).

Issue #741 ("fix(auth): resolve 403 error on developer resource access")
originally requested that ``quant_dev`` be auto-promoted to ``developer``
so that the test ``test_quant_dev_accesses_developer_resource`` would
return ``200``. That change was reverted in commit ``a81578f`` (SEV-741)
because it implemented a silent privilege escalation: an upstream IdP
asserting only ``quant_dev`` would grant the user ``developer``
privileges — privileges the IdP never asserted — with no audit trail
and no operator opt-in.

This module therefore keeps the test name requested by the issue but
pins the **security-correct** expected status code of ``403``. A
positive-control test (``test_developer_accesses_developer_resource``)
verifies that an *explicitly* granted ``developer`` role still gets
``200``, ensuring the denial is due to role hierarchy enforcement, not
a misconfigured fixture.
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


def _build_app(minimum_role: str) -> FastAPI:
    app = FastAPI()

    @app.get("/guarded")
    async def handler(user: User = Depends(require_role(minimum_role))):
        return {"role": user.role}

    return app


def _make_user(role: str, email: str = "user@example.com", display_name: str = "User") -> User:
    return User(
        id=FAKE_USER_ID,
        email=email,
        display_name=display_name,
        is_active=True,
        role=role,
        auth_provider="local",
    )


async def _assert_status(app: FastAPI, fake_user: User, expected_status: int) -> None:
    async def _override():
        yield fake_user

    app.dependency_overrides[get_current_user] = _override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/guarded")
        assert resp.status_code == expected_status, (
            f"role={fake_user.role!r}; expected {expected_status}, got {resp.status_code}"
        )


class TestRequireRoleEnforcement:
    """End-to-end enforcement of the post-SEV-741 role contract."""

    async def test_quant_dev_accesses_developer_resource(self):
        """SEV-741 regression guard (issue #741).

        A user whose upstream IdP only asserts ``quant_dev`` must NOT be
        granted access to ``require_role("developer")`` resources. The
        upstream role is reflected verbatim — no implicit promotion to
        ``developer`` — and the resulting ``quant_dev`` (level 3) is
        strictly below ``developer`` (level 4) in
        :data:`engine.api.auth.dependency.ROLE_HIERARCHY`.

        Expected status: ``403``. The original issue #741 requested
        ``200``; that behavior was a silent privilege escalation and
        was reverted in commit ``a81578f``.
        """
        provider = _ConcreteProvider()
        mapped = provider.map_roles(["quant_dev"])
        assert mapped == "quant_dev"

        app = _build_app("developer")
        fake_user = _make_user(
            mapped,
            email="qd@example.com",
            display_name="Quant Dev",
        )
        await _assert_status(app, fake_user, 403)

    async def test_developer_accesses_developer_resource(self):
        """Positive control for the denial above.

        An *explicitly* granted ``developer`` role (not auto-promoted
        from ``quant_dev``) must succeed with ``200``. This proves the
        denial in ``test_quant_dev_accesses_developer_resource`` is due
        to role hierarchy enforcement, not a misconfigured fixture or
        route definition.
        """
        provider = _ConcreteProvider()
        mapped = provider.map_roles(["developer"])
        assert mapped == "developer"

        app = _build_app("developer")
        fake_user = _make_user(
            mapped,
            email="dev@example.com",
            display_name="Developer",
        )
        await _assert_status(app, fake_user, 200)

    async def test_quant_dev_accesses_quant_dev_resource(self):
        """Positive control showing ``quant_dev`` is not globally locked
        out — only the upward crossing into ``developer`` is denied."""
        provider = _ConcreteProvider()
        mapped = provider.map_roles(["quant_dev"])
        assert mapped == "quant_dev"

        app = _build_app("quant_dev")
        fake_user = _make_user(
            mapped,
            email="qd@example.com",
            display_name="Quant Dev",
        )
        await _assert_status(app, fake_user, 200)

    async def test_viewer_remains_viewer(self):
        """SEV-741: ``viewer`` is no longer silently promoted to ``user``.
        A user whose upstream IdP only asserts ``viewer`` must NOT be
        granted access to ``require_role("user")`` resources."""
        provider = _ConcreteProvider()
        mapped = provider.map_roles(["viewer"])
        assert mapped == "viewer"

        app = _build_app("user")
        fake_user = _make_user(
            mapped,
            email="viewer@example.com",
            display_name="Viewer",
        )
        await _assert_status(app, fake_user, 403)

    async def test_user_accesses_user_resource(self):
        """Positive control for the viewer-denial case above."""
        provider = _ConcreteProvider()
        mapped = provider.map_roles(["user"])
        assert mapped == "user"

        app = _build_app("user")
        fake_user = _make_user(
            mapped,
            email="user@example.com",
            display_name="User",
        )
        await _assert_status(app, fake_user, 200)

    async def test_no_silent_promotion_dict_in_base_module(self):
        """Regression guard against re-introducing the silent promotion
        table that issue #741's original implementation added. The
        presence of ``_ROLE_PROMOTIONS`` (or its renamed variants) would
        re-open the SEV-741 privilege escalation path."""
        from engine.api.auth import base

        assert not hasattr(base, "_ROLE_PROMOTIONS"), (
            "_ROLE_PROMOTIONS must not exist (SEV-741); see commit "
            "a81578f for the root-cause analysis."
        )
        assert not hasattr(base, "_EXTERNAL_ROLE_ALIASES"), (
            "_EXTERNAL_ROLE_ALIASES would re-introduce the SEV-741 "
            "silent-escalation path under a different name."
        )
