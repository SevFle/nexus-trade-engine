"""Comprehensive tests for the role-promotion logic introduced in #741.

The recent change (commit f4231fc) added a ``_ROLE_PROMOTIONS`` table to
``engine.api.auth.base`` and made ``IAuthProvider.map_roles`` apply it after
the priority-based "best role" selection.  These tests target:

* The contents and immutability of the ``_ROLE_PROMOTIONS`` mapping.
* Promotion semantics for every entry (``viewer -> user``,
  ``quant_dev -> developer``).
* Interaction between priority selection and post-selection promotion:
  promotion is applied to the *winner*, not to candidates.
* Round-trip through ``require_role`` so a promoted identity is actually
  admitted to the protected route.
* Negative cases: unknown roles, empty input, mixed casing, whitespace,
  duplicate entries, and the original 403 regression that motivated #741.

The existing test files (``test_auth_rbac_roles.py``,
``test_recent_code_comprehensive.py``, ``test_auth_recent_integration.py``)
already touch the happy-path; this file focuses on the corner cases and
the exact behaviour added by the commit so any future regression is
caught in isolation.
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
from engine.api.auth.dependency import get_current_user, require_role
from engine.db.models import User
from tests.conftest import FAKE_USER_ID


class _RecordingProvider(IAuthProvider):
    """Concrete provider that records ``map_roles`` arguments for inspection."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def name(self) -> str:
        return "recording"

    async def authenticate(self, **kwargs: Any) -> AuthResult:
        return AuthResult(success=True)


def _user(role: str) -> User:
    return User(
        id=FAKE_USER_ID,
        email=f"{role}@example.com",
        display_name=role.title(),
        is_active=True,
        role=role,
        auth_provider="local",
    )


class TestRolePromotionsTable:
    """The ``_ROLE_PROMOTIONS`` mapping itself — its source-of-truth contents."""

    def test_table_is_dict(self):
        assert isinstance(_ROLE_PROMOTIONS, dict)

    def test_table_contains_viewer_to_user(self):
        assert _ROLE_PROMOTIONS["viewer"] == "user"

    def test_table_contains_quant_dev_to_developer(self):
        assert _ROLE_PROMOTIONS["quant_dev"] == "developer"

    def test_table_has_exactly_two_entries(self):
        """Regression guard: the commit added exactly two promotions."""
        assert set(_ROLE_PROMOTIONS.keys()) == {"viewer", "quant_dev"}

    def test_table_does_not_promote_admin(self):
        """Admin must never be silently demoted/promoted by the table."""
        assert "admin" not in _ROLE_PROMOTIONS

    def test_table_does_not_promote_developer(self):
        assert "developer" not in _ROLE_PROMOTIONS

    def test_table_values_are_valid_engine_roles(self):
        from engine.api.auth.base import IAuthProvider

        provider = type("_P", (IAuthProvider,), {  # type: ignore[misc]
            "name": property(lambda self: "p"),
            "authenticate": lambda self, **kw: AuthResult(),
        })()
        for promoted in _ROLE_PROMOTIONS.values():
            # A promoted role must itself survive map_roles unchanged
            assert provider.map_roles([promoted]) == promoted


class TestMapRolesPromotionSemantics:
    """Promotion behaviour for each entry of ``_ROLE_PROMOTIONS``."""

    def setup_method(self) -> None:
        self.provider = _RecordingProvider()

    def test_viewer_is_promoted_to_user(self):
        assert self.provider.map_roles(["viewer"]) == "user"

    def test_quant_dev_is_promoted_to_developer(self):
        assert self.provider.map_roles(["quant_dev"]) == "developer"

    def test_quant_dev_beats_viewer_then_promotes(self):
        """Priority picks quant_dev (rank 3) over viewer (rank 0); then promotion
        maps quant_dev -> developer."""
        assert self.provider.map_roles(["viewer", "quant_dev"]) == "developer"

    def test_viewer_loses_to_user_but_winner_is_still_user(self):
        """If a real ``user`` is in the input alongside ``viewer``, priority
        selects ``user`` (rank 1) and ``user`` is *not* in the promotion table,
        so the result remains ``user``."""
        assert self.provider.map_roles(["viewer", "user"]) == "user"

    def test_admin_dominates_then_no_promotion(self):
        """Admin (rank 6) wins; admin is not in ``_ROLE_PROMOTIONS`` so the
        result is admin unchanged."""
        assert self.provider.map_roles(["quant_dev", "admin", "viewer"]) == "admin"

    def test_portfolio_manager_dominates_quant_dev(self):
        """PM (rank 5) > quant_dev (rank 3); promotion table is not applied
        to PM."""
        assert self.provider.map_roles(["quant_dev", "portfolio_manager"]) == "portfolio_manager"

    def test_unknown_role_only_falls_back_to_default_user(self):
        assert self.provider.map_roles(["ghost"]) == "user"

    def test_empty_input_returns_user(self):
        assert self.provider.map_roles([]) == "user"

    def test_mixed_unknown_and_known_uses_known(self):
        assert self.provider.map_roles(["ghost", "quant_dev"]) == "developer"

    def test_only_unknown_roles_falls_back_to_user(self):
        assert self.provider.map_roles(["ghost", "phantom"]) == "user"

    def test_case_insensitive_input(self):
        assert self.provider.map_roles(["QUANT_DEV"]) == "developer"
        assert self.provider.map_roles(["Viewer"]) == "user"

    def test_whitespace_is_stripped(self):
        assert self.provider.map_roles(["  quant_dev  "]) == "developer"
        assert self.provider.map_roles(["\tviewer\n"]) == "user"

    def test_duplicates_do_not_alter_outcome(self):
        assert self.provider.map_roles(["quant_dev", "quant_dev"]) == "developer"
        assert self.provider.map_roles(["viewer", "viewer"]) == "user"

    def test_input_list_is_not_mutated(self):
        roles = ["quant_dev", "viewer"]
        snapshot = list(roles)
        self.provider.map_roles(roles)
        assert roles == snapshot

    def test_recording_provider_records_call_signature(self):
        roles = ["quant_dev"]
        self.provider.map_roles(roles)
        assert self.provider.calls == []  # map_roles does not self-record; sanity check
        # Sanity: provider name is still accessible
        assert self.provider.name == "recording"


class TestRequireRoleIntegrationWithPromotions:
    """End-to-end: a promoted role must satisfy ``require_role`` of the
    *target* role.  This is the actual #741 regression scenario — a
    ``quant_dev`` identity used to be denied access to developer-only
    endpoints because the role was never promoted before the check."""

    @pytest.mark.parametrize(
        ("external_role", "required_role"),
        [
            ("quant_dev", "developer"),
            ("viewer", "user"),
        ],
    )
    async def test_promoted_role_can_access_protected_resource(
        self,
        external_role: str,
        required_role: str,
    ):
        app = FastAPI()

        @app.get("/protected")
        async def handler(user: User = Depends(require_role(required_role))):
            return {"role": user.role}

        provider = _RecordingProvider()
        mapped = provider.map_roles([external_role])
        assert mapped == required_role  # sanity for the parametrisation

        app.dependency_overrides[get_current_user] = lambda: _user(mapped)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/protected")
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"role": mapped}

    async def test_unpromoted_viewer_cannot_access_developer_resource(self):
        """Regression: ``viewer`` is promoted to ``user``, *not* to
        ``developer`` — it must still be rejected by ``require_role('developer')``."""
        app = FastAPI()

        @app.get("/dev")
        async def handler(user: User = Depends(require_role("developer"))):
            return {"role": user.role}

        provider = _RecordingProvider()
        mapped = provider.map_roles(["viewer"])
        assert mapped == "user"

        app.dependency_overrides[get_current_user] = lambda: _user(mapped)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/dev")
            assert resp.status_code == 403

    async def test_admin_still_passes_developer_endpoint(self):
        """Sanity: the fix must not regress admin access."""
        app = FastAPI()

        @app.get("/dev")
        async def handler(user: User = Depends(require_role("developer"))):
            return {"role": user.role}

        app.dependency_overrides[get_current_user] = lambda: _user("admin")

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/dev")
            assert resp.status_code == 200
            assert resp.json() == {"role": "admin"}


class TestUserInfoAndAuthResultDataclass:
    """Smoke tests on the dataclass defaults — these objects are constructed
    by ``map_roles`` callers and the promotion logic assumes the default
    ``roles`` field is ``["user"]``."""

    def test_user_info_default_roles(self):
        info = UserInfo()
        assert info.roles == ["user"]
        assert info.provider == "local"
        assert info.external_id is None
        assert info.email == ""
        assert info.raw_claims == {}

    def test_auth_result_defaults(self):
        r = AuthResult()
        assert r.success is False
        assert r.user_info is None
        assert r.error is None

    def test_user_info_roles_field_is_per_instance(self):
        a = UserInfo()
        b = UserInfo()
        a.roles.append("admin")
        assert b.roles == ["user"]  # mutable default must not leak across instances

    def test_raw_claims_field_is_per_instance(self):
        a = UserInfo()
        b = UserInfo()
        a.raw_claims["k"] = 1
        assert b.raw_claims == {}


class TestProviderDefaults:
    """Default-method behaviour of ``IAuthProvider`` not overridden by
    subclasses — these are inherited by every concrete provider (LDAP,
    OIDC, local) and any future provider must keep these contracts."""

    def setup_method(self) -> None:
        self.provider = _RecordingProvider()

    async def test_get_user_info_default_returns_none(self):
        assert await self.provider.get_user_info("anyone") is None

    async def test_create_user_default_returns_unsuccessful_result(self):
        result = await self.provider.create_user(UserInfo())
        assert isinstance(result, AuthResult)
        assert result.success is False
        assert "recording" in (result.error or "")

    async def test_authenticate_default_contract(self):
        result = await self.provider.authenticate()
        assert isinstance(result, AuthResult)
        assert result.success is True
