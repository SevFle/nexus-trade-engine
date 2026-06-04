"""Comprehensive tests for the external-to-internal role mapping pipeline.

These tests target the most recently changed code in ``engine/api/auth/base.py``
(commit f4231fc, "fix(auth): resolve 403 error on developer resource access").

Mapping has two phases that must remain cleanly separated:

1. **External alias resolution** — IdP-supplied names like ``viewer`` and
   ``quant_dev`` are translated to their canonical internal names
   (``user`` and ``developer``) via :data:`_EXTERNAL_ROLE_ALIASES`.
2. **Internal hierarchy selection** — the highest-priority canonical name wins.

The tests below cover:

- Each individual role (positive mapping).
- Mixed lists where both aliases and canonical names appear together.
- Non-monotonic guard: adding a role to the input MUST NEVER lower the
  returned privilege. This is the invariant the post-hoc promotion design
  in the previous implementation subtly relied on but never enforced.
- Case insensitivity, whitespace handling, duplicate suppression.
- Empty input, all-unknown input, all-alias input.
- Upward-only invariant on :data:`_EXTERNAL_ROLE_ALIASES` itself.
- Integration with ``require_role`` via FastAPI's dependency injection.
"""

from __future__ import annotations

import secrets

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from engine.api.auth.base import (
    _EXTERNAL_ROLE_ALIASES,
    _ROLE_PRIORITY,
    AuthResult,
    IAuthProvider,
    UserInfo,
)
from engine.api.auth.dependency import ROLE_HIERARCHY, get_current_user, require_role
from engine.db.models import User
from tests.conftest import FAKE_USER_ID


class _ConcreteProvider(IAuthProvider):
    """Minimal concrete subclass for unit-testing IAuthProvider behaviour."""

    @property
    def name(self) -> str:
        return "concrete-test"

    async def authenticate(self, **kwargs):  # pragma: no cover - not exercised here
        return AuthResult(success=True)


@pytest.fixture
def provider() -> IAuthProvider:
    return _ConcreteProvider()


# ---------------------------------------------------------------------------
# Phase-1: external alias resolution (single-role inputs)
# ---------------------------------------------------------------------------


class TestExternalAliasResolution:
    """Each entry in _EXTERNAL_ROLE_ALIASES should resolve on its own."""

    @pytest.mark.parametrize(
        ("external", "internal"),
        sorted(_EXTERNAL_ROLE_ALIASES.items()),
    )
    def test_single_alias_resolves_to_canonical(self, provider, external, internal):
        assert provider.map_roles([external]) == internal

    def test_only_known_aliases_present(self):
        # Anyone adding a new alias MUST also add it to ROLE_HIERARCHY so the
        # require_role dependency can reason about it.
        for internal in _EXTERNAL_ROLE_ALIASES.values():
            assert internal in ROLE_HIERARCHY, (
                f"alias target {internal!r} missing from ROLE_HIERARCHY"
            )

    def test_only_known_canonical_targets(self):
        for internal in _EXTERNAL_ROLE_ALIASES.values():
            assert internal in _ROLE_PRIORITY


class TestCanonicalRolePassThrough:
    """Canonical internal role names should map to themselves unchanged."""

    @pytest.mark.parametrize(
        "role",
        sorted(_ROLE_PRIORITY.keys()),
    )
    def test_canonical_role_maps_to_itself(self, provider, role):
        # ``viewer`` and ``quant_dev`` ARE keys in _ROLE_PRIORITY but they are
        # aliases, so they get translated. Every other role is a pass-through.
        expected = _EXTERNAL_ROLE_ALIASES.get(role, role)
        assert provider.map_roles([role]) == expected


# ---------------------------------------------------------------------------
# Phase-2: hierarchy selection (multi-role inputs)
# ---------------------------------------------------------------------------


class TestHierarchySelection:
    def test_admin_wins_over_all(self, provider):
        all_roles = sorted(_ROLE_PRIORITY.keys())
        assert provider.map_roles(all_roles) == "admin"

    def test_developer_beats_user_and_aliases(self, provider):
        assert provider.map_roles(["user", "viewer", "developer"]) == "developer"

    def test_portfolio_manager_beats_developer(self, provider):
        assert provider.map_roles(["developer", "portfolio_manager"]) == "portfolio_manager"

    def test_retail_trader_beats_user(self, provider):
        assert provider.map_roles(["user", "retail_trader"]) == "retail_trader"


# ---------------------------------------------------------------------------
# Mixed alias + canonical lists (the case the original bug surfaced on)
# ---------------------------------------------------------------------------


class TestMixedAliasAndCanonical:
    """Exercises the precise scenario from commit f4231fc.

    User belongs to LDAP/OIDC groups that emit BOTH an external alias and
    canonical internal roles. The mapper must walk both, promote aliases
    to canonical form, and then pick the highest-privilege canonical role.
    """

    def test_quant_dev_alone_promotes_to_developer(self, provider):
        # The regression case from #741: a user with only the quant_dev
        # group must reach the developer role so they can access
        # require_role("developer") endpoints.
        assert provider.map_roles(["quant_dev"]) == "developer"

    def test_viewer_alone_promotes_to_user(self, provider):
        assert provider.map_roles(["viewer"]) == "user"

    def test_quant_dev_with_lower_canonical(self, provider):
        # quant_dev -> developer (4) beats retail_trader (2) and user (1).
        assert provider.map_roles(["retail_trader", "quant_dev"]) == "developer"

    def test_quant_dev_with_higher_canonical(self, provider):
        # portfolio_manager (5) beats quant_dev -> developer (4).
        assert provider.map_roles(["portfolio_manager", "quant_dev"]) == "portfolio_manager"

    def test_both_aliases_together(self, provider):
        # viewer -> user (1) and quant_dev -> developer (4) — developer wins.
        assert provider.map_roles(["viewer", "quant_dev"]) == "developer"

    def test_alias_with_explicit_canonical_counterpart(self, provider):
        # IdP that emits both "viewer" and "user" — the canonical "user" is
        # already there so the alias should be a no-op.
        assert provider.map_roles(["viewer", "user"]) == "user"
        assert provider.map_roles(["quant_dev", "developer"]) == "developer"

    def test_alias_with_admin(self, provider):
        # admin (6) beats every alias.
        assert provider.map_roles(["viewer", "quant_dev", "admin"]) == "admin"

    def test_alias_with_portfolio_manager(self, provider):
        # portfolio_manager (5) beats quant_dev -> developer (4) but viewer
        # -> user (1) is below. Result must be portfolio_manager.
        assert provider.map_roles(["viewer", "quant_dev", "portfolio_manager"]) == (
            "portfolio_manager"
        )

    def test_alias_with_retail_trader(self, provider):
        # retail_trader (2) beats viewer -> user (1) but is below quant_dev
        # -> developer (4). Both aliases together should still pick developer.
        assert provider.map_roles(["viewer", "retail_trader"]) == "retail_trader"
        assert provider.map_roles(["quant_dev", "retail_trader"]) == "developer"
        assert provider.map_roles(["viewer", "quant_dev", "retail_trader"]) == "developer"


# ---------------------------------------------------------------------------
# Non-monotonic guard — the invariant the post-hoc design relied on
# ---------------------------------------------------------------------------


class TestMonotonicityInvariant:
    """Adding a role to the input MUST NEVER decrease the returned priority.

    The pre-refactor design performed promotion AFTER max-selection. That made
    the function monotonic only because every entry in ``_ROLE_PROMOTIONS``
    happened to map upward. These tests pin that invariant so a future
    contributor adding a downward alias doesn't silently introduce a
    privilege-reduction bug.
    """

    @pytest.mark.parametrize(
        "roles",
        [
            [],
            ["viewer"],
            ["user"],
            ["quant_dev"],
            ["developer"],
            ["retail_trader"],
            ["portfolio_manager"],
            ["admin"],
            ["viewer", "quant_dev"],
            ["retail_trader", "quant_dev"],
            ["portfolio_manager", "quant_dev"],
            ["viewer", "admin"],
        ],
    )
    def test_adding_role_never_lowers_priority(self, provider, roles):
        baseline = provider.map_roles(roles)
        baseline_priority = _ROLE_PRIORITY[baseline]
        for extra in _ROLE_PRIORITY:
            augmented = [*roles, extra]
            result = provider.map_roles(augmented)
            result_priority = _ROLE_PRIORITY[result]
            assert result_priority >= baseline_priority, (
                f"non-monotonic: {roles!r} -> {baseline!r} ({baseline_priority}) "
                f"but {augmented!r} -> {result!r} ({result_priority})"
            )

    def test_alias_targets_always_higher_priority(self):
        # Structural invariant on _EXTERNAL_ROLE_ALIASES itself: every alias
        # maps to a strictly higher-priority canonical name.
        for external, internal in _EXTERNAL_ROLE_ALIASES.items():
            assert external in _ROLE_PRIORITY, (
                f"alias source {external!r} must be in _ROLE_PRIORITY"
            )
            assert internal in _ROLE_PRIORITY, (
                f"alias target {internal!r} must be in _ROLE_PRIORITY"
            )
            assert _ROLE_PRIORITY[internal] > _ROLE_PRIORITY[external], (
                f"alias {external!r} -> {internal!r} lowers priority "
                f"({_ROLE_PRIORITY[external]} -> {_ROLE_PRIORITY[internal]}); "
                "aliases must be strictly upward"
            )


# ---------------------------------------------------------------------------
# Normalisation: case, whitespace, duplicates, empty
# ---------------------------------------------------------------------------


class TestInputNormalisation:
    @pytest.mark.parametrize("role", ["viewer", "VIEWER", "Viewer", "ViEwEr"])
    def test_case_insensitive(self, provider, role):
        assert provider.map_roles([role]) == "user"

    @pytest.mark.parametrize("role", ["quant_dev", "QUANT_DEV", "Quant_Dev"])
    def test_case_insensitive_quant_dev(self, provider, role):
        assert provider.map_roles([role]) == "developer"

    @pytest.mark.parametrize(
        "role",
        ["  viewer  ", "\tviewer\t", " viewer", "quant_dev ", "  quant_dev  "],
    )
    def test_whitespace_stripped(self, provider, role):
        expected = "developer" if "quant_dev" in role else "user"
        assert provider.map_roles([role]) == expected

    def test_empty_string_role_ignored(self, provider):
        assert provider.map_roles([""]) == "user"

    def test_whitespace_only_role_ignored(self, provider):
        # After strip() this becomes "" which is not in _ROLE_PRIORITY.
        assert provider.map_roles(["   "]) == "user"

    def test_duplicate_roles_collapsed(self, provider):
        assert provider.map_roles(["admin", "admin", "admin"]) == "admin"
        assert provider.map_roles(["viewer", "viewer", "viewer"]) == "user"
        assert provider.map_roles(["quant_dev", "quant_dev"]) == "developer"

    def test_empty_list_returns_default(self, provider):
        assert provider.map_roles([]) == "user"

    def test_all_unknown_returns_default(self, provider):
        assert provider.map_roles(["superadmin", "guest", "root"]) == "user"

    def test_unknown_does_not_mask_known(self, provider):
        # Unknown roles interspersed with known ones must not break the loop.
        assert provider.map_roles(["unknown", "admin", "bogus"]) == "admin"
        assert provider.map_roles(["bogus", "viewer", "zzz"]) == "user"


# ---------------------------------------------------------------------------
# Determinism / order-independence
# ---------------------------------------------------------------------------


class TestOrderIndependence:
    """map_roles must be a pure function of the input SET, not its order."""

    @pytest.mark.parametrize(
        "roles",
        [
            ["viewer", "quant_dev"],
            ["quant_dev", "viewer"],
            ["user", "developer", "admin"],
            ["admin", "developer", "user"],
            ["retail_trader", "quant_dev", "viewer"],
            ["viewer", "retail_trader", "quant_dev"],
        ],
    )
    def test_permutations_yield_same_result(self, provider, roles):
        baseline = provider.map_roles(roles)
        for _ in range(10):
            shuffled = list(roles)
            # Deterministic shuffle for repeatability without using the
            # insecure stdlib random module (ruff S311).
            for i in range(len(shuffled) - 1, 0, -1):
                j = secrets.randbelow(i + 1)
                shuffled[i], shuffled[j] = shuffled[j], shuffled[i]
            assert provider.map_roles(shuffled) == baseline


# ---------------------------------------------------------------------------
# Integration with require_role via FastAPI dependency override
# ---------------------------------------------------------------------------


def _make_user(role: str) -> User:
    return User(
        id=FAKE_USER_ID,
        email="mapped@example.com",
        display_name="Mapped",
        is_active=True,
        role=role,
        auth_provider="oidc",
    )


class TestRequireRoleIntegration:
    """Walks the realistic OIDC flow: IdP sends raw groups → map_roles →
    the resulting role must satisfy require_role checks at the right level."""

    async def test_quant_dev_group_grants_developer_endpoint(self):
        app = FastAPI()

        @app.get("/dev-only")
        async def handler(user: User = Depends(require_role("developer"))):
            return {"role": user.role}

        mapped = _ConcreteProvider().map_roles(["quant_dev"])
        assert mapped == "developer"
        user = _make_user(mapped)

        async def _override():
            yield user

        app.dependency_overrides[get_current_user] = _override
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/dev-only")
            assert resp.status_code == 200
            assert resp.json() == {"role": "developer"}

    async def test_quant_dev_group_denied_portfolio_manager_endpoint(self):
        app = FastAPI()

        @app.get("/pm-only")
        async def handler(user: User = Depends(require_role("portfolio_manager"))):
            return {"role": user.role}

        mapped = _ConcreteProvider().map_roles(["quant_dev"])
        user = _make_user(mapped)

        async def _override():
            yield user

        app.dependency_overrides[get_current_user] = _override
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/pm-only")
            assert resp.status_code == 403

    async def test_viewer_group_grants_user_endpoint(self):
        app = FastAPI()

        @app.get("/user-only")
        async def handler(user: User = Depends(require_role("user"))):
            return {"role": user.role}

        mapped = _ConcreteProvider().map_roles(["viewer"])
        assert mapped == "user"
        user = _make_user(mapped)

        async def _override():
            yield user

        app.dependency_overrides[get_current_user] = _override
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/user-only")
            assert resp.status_code == 200
            assert resp.json() == {"role": "user"}

    async def test_admin_alias_chain_grants_admin(self):
        # Mix a low alias with the admin role: admin must win.
        app = FastAPI()

        @app.get("/admin-only")
        async def handler(user: User = Depends(require_role("admin"))):
            return {"role": user.role}

        mapped = _ConcreteProvider().map_roles(["viewer", "quant_dev", "admin"])
        assert mapped == "admin"
        user = _make_user(mapped)

        async def _override():
            yield user

        app.dependency_overrides[get_current_user] = _override
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/admin-only")
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Defaults & dataclass contracts that the mapper depends on
# ---------------------------------------------------------------------------


class TestDataclassDefaults:
    def test_userinfo_default_role(self):
        info = UserInfo()
        assert info.roles == ["user"]

    def test_authresult_defaults(self):
        r = AuthResult()
        assert r.success is False
        assert r.user_info is None
        assert r.error is None
