"""Comprehensive tests for the role-promotion change in ``engine/api/auth/base.py``
(commit f4231fc / PR #741).

The change introduced:

1.  ``_ROLE_PROMOTIONS`` mapping: ``viewer`` -> ``user`` and ``quant_dev`` ->
    ``developer``.
2.  ``IAuthProvider.map_roles`` applies that mapping to the highest-priority
    role it selects from the input list.

These tests cover the promotion behaviour exhaustively, the surrounding
``UserInfo`` / ``AuthResult`` dataclasses, and the default implementations of
``get_user_info`` / ``create_user`` that the change touches indirectly by
sharing the same module.
"""

from __future__ import annotations

import pytest

from engine.api.auth.base import (
    _ROLE_PROMOTIONS,
    AuthResult,
    IAuthProvider,
    UserInfo,
)


class _ConcreteProvider(IAuthProvider):
    """Minimal concrete subclass for testing the abstract base."""

    @property
    def name(self) -> str:
        return "concrete-test"

    async def authenticate(self, **kwargs: object) -> AuthResult:
        return AuthResult(success=True)


@pytest.fixture
def provider() -> _ConcreteProvider:
    return _ConcreteProvider()


# ---------------------------------------------------------------------------
# _ROLE_PROMOTIONS constant
# ---------------------------------------------------------------------------


class TestRolePromotionsConstant:
    """Guarantees the promotion table exposed by the module is correct."""

    def test_viewer_promoted_to_user(self):
        assert _ROLE_PROMOTIONS["viewer"] == "user"

    def test_quant_dev_promoted_to_developer(self):
        assert _ROLE_PROMOTIONS["quant_dev"] == "developer"

    def test_only_two_promotions_defined(self):
        assert set(_ROLE_PROMOTIONS.keys()) == {"viewer", "quant_dev"}

    def test_promotion_targets_are_canonical_roles(self):
        # Promoted values must themselves be valid (non-promoted) role names
        # so map_roles is idempotent.
        for target in _ROLE_PROMOTIONS.values():
            assert target not in _ROLE_PROMOTIONS

    def test_promotion_targets_are_priority_roles(self):
        # Targets are real priority-table keys, not arbitrary strings.

        # Inspect the priority dict by calling map_roles with each target
        # alone — it must select itself.
        for target in _ROLE_PROMOTIONS.values():
            class _P(IAuthProvider):
                @property
                def name(self) -> str:
                    return "x"

                async def authenticate(self, **kwargs: object) -> AuthResult:
                    return AuthResult()

            assert _P().map_roles([target]) == target


# ---------------------------------------------------------------------------
# map_roles: promotion behaviour
# ---------------------------------------------------------------------------


class TestMapRolesPromotion:
    """Direct coverage of the ``_ROLE_PROMOTIONS.get(best, best)`` line."""

    def test_viewer_alone_is_promoted_to_user(self, provider):
        assert provider.map_roles(["viewer"]) == "user"

    def test_quant_dev_alone_is_promoted_to_developer(self, provider):
        assert provider.map_roles(["quant_dev"]) == "developer"

    def test_viewer_with_lower_unknown_role_still_promotes(self, provider):
        # Unknown roles are ignored; viewer is highest known -> promoted.
        assert provider.map_roles(["viewer", "superuser"]) == "user"

    def test_quant_dev_with_lower_unknown_role_still_promotes(self, provider):
        assert provider.map_roles(["quant_dev", "ghost"]) == "developer"

    @pytest.mark.parametrize(
        "higher",
        ["user", "retail_trader", "developer", "portfolio_manager", "admin"],
    )
    def test_viewer_loses_against_higher_known_role(self, provider, higher):
        # When a higher-priority role is present, promotion of viewer never
        # fires because "best" is no longer "viewer".
        assert provider.map_roles(["viewer", higher]) == higher

    @pytest.mark.parametrize(
        "higher",
        ["developer", "portfolio_manager", "admin"],
    )
    def test_quant_dev_loses_against_higher_known_role(self, provider, higher):
        assert provider.map_roles(["quant_dev", higher]) == higher

    def test_quant_dev_plus_developer_picks_developer_directly(self, provider):
        # developer (priority 4) > quant_dev (priority 3) — no promotion
        # needed but the result still matches the promoted target.
        assert provider.map_roles(["quant_dev", "developer"]) == "developer"

    def test_viewer_plus_user_picks_user_without_needing_promotion(
        self, provider
    ):
        # user (priority 1) > viewer (priority 0) — "best" becomes "user"
        # which is not in _ROLE_PROMOTIONS.
        assert provider.map_roles(["viewer", "user"]) == "user"

    def test_promotion_is_idempotent(self, provider):
        # Re-mapping the promoted result must not change it again.
        first = provider.map_roles(["viewer"])
        assert first == "user"
        second = provider.map_roles([first])
        assert second == "user"

        first_q = provider.map_roles(["quant_dev"])
        assert first_q == "developer"
        second_q = provider.map_roles([first_q])
        assert second_q == "developer"

    def test_promotion_respects_case_insensitive_normalisation(
        self, provider
    ):
        assert provider.map_roles(["VIEWER"]) == "user"
        assert provider.map_roles(["QUANT_DEV"]) == "developer"
        assert provider.map_roles(["Quant_Dev"]) == "developer"

    def test_promotion_respects_whitespace_stripping(self, provider):
        assert provider.map_roles(["  viewer  "]) == "user"
        assert provider.map_roles(["\tquant_dev\n"]) == "developer"

    def test_promotion_with_mixed_case_and_whitespace(self, provider):
        assert provider.map_roles(["  Viewer ", "Admin"]) == "admin"

    def test_order_independence_viewer(self, provider):
        a = provider.map_roles(["viewer", "user"])
        b = provider.map_roles(["user", "viewer"])
        assert a == b == "user"

    def test_order_independence_quant_dev(self, provider):
        a = provider.map_roles(["quant_dev", "developer"])
        b = provider.map_roles(["developer", "quant_dev"])
        assert a == b == "developer"

    def test_duplicates_do_not_change_promotion(self, provider):
        assert provider.map_roles(["viewer", "viewer", "viewer"]) == "user"
        assert provider.map_roles(["quant_dev"] * 5) == "developer"

    def test_empty_list_falls_back_to_default_user(self, provider):
        assert provider.map_roles([]) == "user"

    def test_only_unknown_roles_falls_back_to_default_user(self, provider):
        assert provider.map_roles(["ghost", "alien"]) == "user"

    @pytest.mark.parametrize(
        ("roles", "expected"),
        [
            (["viewer"], "user"),
            (["quant_dev"], "developer"),
            (["viewer", "quant_dev"], "developer"),
            (["viewer", "quant_dev", "admin"], "admin"),
            (["retail_trader", "viewer"], "retail_trader"),
            (["portfolio_manager", "quant_dev"], "portfolio_manager"),
        ],
    )
    def test_combination_matrix(self, provider, roles, expected):
        assert provider.map_roles(roles) == expected

    def test_promotion_dict_is_read_at_call_time(self, provider, monkeypatch):
        # map_roles must consult the live module-level mapping rather than
        # snapshotting it at import time. We mutate the dict in-place and
        # verify the result changes accordingly. Uses quant_dev because the
        # viewer entry is unreachable in practice (user has higher priority
        # and is the default starting value for ``best``).
        import engine.api.auth.base as base_mod

        monkeypatch.setitem(
            base_mod._ROLE_PROMOTIONS, "quant_dev", "portfolio_manager"
        )
        assert provider.map_roles(["quant_dev"]) == "portfolio_manager"

    def test_viewer_promotion_entry_is_dead_code(self, provider):
        # Documents current behaviour: ``viewer`` has priority 0 while the
        # default ``best = "user"`` has priority 1, so best never becomes
        # ``"viewer"`` and the viewer entry in _ROLE_PROMOTIONS is never
        # consulted. map_roles(["viewer"]) returns "user" by virtue of the
        # default, not the promotion.
        import engine.api.auth.base as base_mod

        assert "viewer" in base_mod._ROLE_PROMOTIONS
        # Even when we remove the entry, the result is the same.
        original = base_mod._ROLE_PROMOTIONS.pop("viewer")
        try:
            assert provider.map_roles(["viewer"]) == "user"
        finally:
            base_mod._ROLE_PROMOTIONS["viewer"] = original


# ---------------------------------------------------------------------------
# map_roles: priority ordering (unchanged behaviour must keep working)
# ---------------------------------------------------------------------------


class TestMapRolesPrioritySemantics:
    """The promotion change must not regress priority selection."""

    @pytest.mark.parametrize(
        ("roles", "expected"),
        [
            (["admin"], "admin"),
            (["portfolio_manager"], "portfolio_manager"),
            (["developer"], "developer"),
            (["retail_trader"], "retail_trader"),
            (["user"], "user"),
            (["admin", "viewer"], "admin"),
            (["developer", "quant_dev"], "developer"),
            (["user", "developer", "admin"], "admin"),
        ],
    )
    def test_priority_selection(self, provider, roles, expected):
        assert provider.map_roles(roles) == expected

    def test_admin_beats_everything(self, provider):
        everything = [
            "viewer",
            "user",
            "retail_trader",
            "quant_dev",
            "developer",
            "portfolio_manager",
            "admin",
        ]
        assert provider.map_roles(everything) == "admin"


# ---------------------------------------------------------------------------
# Default implementations on IAuthProvider
# ---------------------------------------------------------------------------


class TestIAuthProviderDefaults:
    """Cover the default methods that share the module with the promotion."""

    async def test_get_user_info_default_returns_none(self, provider):
        assert await provider.get_user_info("anyone") is None

    async def test_create_user_default_returns_failure(self, provider):
        result = await provider.create_user(UserInfo(email="x@y.z"))
        assert isinstance(result, AuthResult)
        assert result.success is False
        assert "concrete-test" in (result.error or "")

    async def test_create_user_default_error_mentions_provider_name(
        self, provider
    ):
        result = await provider.create_user(UserInfo())
        assert result.error is not None
        assert provider.name in result.error

    def test_cannot_instantiate_abstract_base_directly(self):
        with pytest.raises(TypeError):
            IAuthProvider()  # type: ignore[abstract]


# --- Dataclasses: UserInfo / AuthResult ---


class TestUserInfoDataclass:
    def test_default_factory_roles_is_user(self):
        u = UserInfo()
        assert u.roles == ["user"]

    def test_default_factories_are_independent_per_instance(self):
        a = UserInfo()
        b = UserInfo()
        a.roles.append("admin")
        assert b.roles == ["user"]

    def test_email_defaults_to_empty(self):
        assert UserInfo().email == ""

    def test_display_name_defaults_to_empty(self):
        assert UserInfo().display_name == ""

    def test_provider_defaults_to_local(self):
        assert UserInfo().provider == "local"

    def test_external_id_defaults_to_none(self):
        assert UserInfo().external_id is None

    def test_raw_claims_default_to_empty_dict(self):
        assert UserInfo().raw_claims == {}

    def test_raw_claims_dicts_are_independent(self):
        a = UserInfo()
        b = UserInfo()
        a.raw_claims["k"] = 1
        assert b.raw_claims == {}

    def test_can_override_all_fields(self):
        u = UserInfo(
            external_id="ext-1",
            email="a@b.c",
            display_name="A",
            provider="oidc",
            roles=["admin"],
            raw_claims={"sub": "x"},
        )
        assert u.external_id == "ext-1"
        assert u.email == "a@b.c"
        assert u.display_name == "A"
        assert u.provider == "oidc"
        assert u.roles == ["admin"]
        assert u.raw_claims == {"sub": "x"}


class TestAuthResultDataclass:
    def test_defaults(self):
        r = AuthResult()
        assert r.success is False
        assert r.user_info is None
        assert r.error is None

    def test_can_carry_user_info(self):
        u = UserInfo(email="z@z.z")
        r = AuthResult(success=True, user_info=u)
        assert r.success is True
        assert r.user_info is u

    def test_can_carry_error(self):
        r = AuthResult(success=False, error="nope")
        assert r.error == "nope"


# ---------------------------------------------------------------------------
# Regression: provider name surfaces in error path
# ---------------------------------------------------------------------------


class TestProviderNameSurface:
    """A provider's ``name`` property is used in the default ``create_user``
    error message, so subclasses that override ``name`` are covered."""

    def test_name_is_readable(self, provider):
        assert provider.name == "concrete-test"

    async def test_create_user_error_uses_overridden_name(self):
        class _Named(IAuthProvider):
            @property
            def name(self) -> str:
                return "ldap-prod"

            async def authenticate(self, **kwargs: object) -> AuthResult:
                return AuthResult()

        result = await _Named().create_user(UserInfo())
        assert "ldap-prod" in (result.error or "")


# ---------------------------------------------------------------------------
# Integration: promotion → require_role end-to-end
# ---------------------------------------------------------------------------


class TestPromotionEndToEnd:
    """Verify the promoted roles satisfy downstream ``require_role`` gates."""

    @pytest.mark.parametrize(
        ("external", "minimum", "allowed"),
        [
            (["viewer"], "user", True),
            (["viewer"], "retail_trader", False),
            (["quant_dev"], "developer", True),
            (["quant_dev"], "portfolio_manager", False),
            (["viewer", "quant_dev"], "developer", True),
            (["viewer", "quant_dev"], "portfolio_manager", False),
        ],
    )
    async def test_promoted_role_satisfies_require_role(
        self, external, minimum, allowed, provider
    ):
        from fastapi import Depends, FastAPI
        from httpx import ASGITransport, AsyncClient

        from engine.api.auth.dependency import get_current_user, require_role
        from engine.db.models import User
        from tests.conftest import FAKE_USER_ID

        mapped = provider.map_roles(external)

        app = FastAPI()

        @app.get("/gated")
        async def handler(user: User = Depends(require_role(minimum))):
            return {"role": user.role}

        fake_user = User(
            id=FAKE_USER_ID,
            email=f"{mapped}@example.com",
            display_name=mapped,
            is_active=True,
            role=mapped,
            auth_provider="local",
        )

        async def _override():
            yield fake_user

        app.dependency_overrides[get_current_user] = _override

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            resp = await ac.get("/gated")

        if allowed:
            assert resp.status_code == 200, resp.text
            assert resp.json()["role"] == mapped
        else:
            assert resp.status_code == 403
