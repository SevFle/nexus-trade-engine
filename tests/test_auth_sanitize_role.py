"""Tests for ``_sanitize_role`` / ``ALLOWED_ROLES`` (role-spoofing hardening).

A hostile or misconfigured upstream Identity Provider (IdP) can send an
arbitrary role string in a federated login. ``_sanitize_role`` is the
defence-in-depth layer that collapses anything which is not an exact,
canonical member of :data:`ALLOWED_ROLES` to the safe default ``"user"``.

The five scenarios pinned here:

1. valid roles pass through verbatim,
2. a spoofed/hybrid ``admin`` claim from a hostile IdP collapses to ``user``,
3. garbage strings collapse to ``user``,
4. a Unicode fullwidth look-alike of ``admin`` collapses to ``user`` (NFKC
   would otherwise turn it into ``admin`` -- a privilege-escalation spoof),
5. an empty string collapses to ``user``.

Additional guards (DoS length cap, control-char injection) are also covered.
"""

from __future__ import annotations

import pytest

from engine.api.auth.base import ALLOWED_ROLES, _sanitize_role

# Fullwidth-Unicode look-alikes (intentionally non-ASCII). Spelled out as
# explicit code points so the source stays ASCII-clean for the linter.
_FULLWIDTH_ADMIN = "\uff41\uff44\uff4d\uff49\uff4e"  # "admin" in fullwidth
_FULLWIDTH_USER = "\uff55\uff53\uff45\uff52"  # "user" in fullwidth


class TestAllowedRolesConstant:
    def test_allowed_roles_is_a_frozenset(self):
        assert isinstance(ALLOWED_ROLES, frozenset)

    def test_allowed_roles_contains_expected_members(self):
        assert (
            frozenset({"user", "viewer", "developer", "portfolio_manager", "admin"})
            == ALLOWED_ROLES
        )


class TestSanitizeRoleValidRolesPass:
    """Scenario 1: every canonical allowed role is returned verbatim."""

    @pytest.mark.parametrize("role", sorted(ALLOWED_ROLES))
    def test_valid_role_passes_through(self, role):
        assert _sanitize_role(role) == role


class TestSanitizeRoleHostileAdminCollapses:
    """Scenario 2: an ``admin`` claim from a hostile IdP that is not the
    exact canonical string collapses to ``user``."""

    @pytest.mark.parametrize(
        "hostile",
        [
            "Admin",  # wrong case
            "ADMIN",  # all-caps
            "administrator",  # a different word entirely
            " admin",  # leading whitespace
            "admin ",  # trailing whitespace
            "admin\x00",  # embedded NUL
            "admin\n",  # embedded newline
            "super-admin",  # invented privilege
            "root",  # invented privilege
        ],
    )
    def test_hostile_admin_collapses_to_user(self, hostile):
        assert _sanitize_role(hostile) == "user"


class TestSanitizeRoleGarbageCollapses:
    """Scenario 3: arbitrary garbage strings collapse to ``user``."""

    @pytest.mark.parametrize(
        "garbage",
        [
            "xyz",
            "!@#$%",
            "not-a-role",
            "12345",
            "guest",
            "superuser",
            "role=admin",
        ],
    )
    def test_garbage_collapses_to_user(self, garbage):
        assert _sanitize_role(garbage) == "user"


class TestSanitizeRoleFullwidthSpoofCollapses:
    """Scenario 4: a Unicode fullwidth look-alike must NOT become ``admin``.

    NFKC normalization alone would turn the fullwidth form into ``admin`` --
    accepting that would let a hostile IdP escalate to admin via a spoof. The
    spoofing guard collapses it to ``user`` instead.
    """

    @pytest.mark.parametrize(
        "spoof",
        [_FULLWIDTH_ADMIN, _FULLWIDTH_USER],
    )
    def test_fullwidth_spoof_collapses_to_user(self, spoof):
        assert _sanitize_role(spoof) == "user"

    def test_fullwidth_admin_is_not_accepted_as_admin(self):
        assert _sanitize_role(_FULLWIDTH_ADMIN) != "admin"


class TestSanitizeRoleEmptyAndNoneCollapse:
    """Scenario 5: empty / missing input collapses to ``user``."""

    @pytest.mark.parametrize("empty", ["", "   ", "\t", None])
    def test_empty_or_none_collapses_to_user(self, empty):
        assert _sanitize_role(empty) == "user"


class TestSanitizeRoleDosGuard:
    """Oversize input is rejected before any regex runs (regex DoS)."""

    def test_oversize_input_collapses_to_user(self):
        assert _sanitize_role("a" * 200) == "user"

    def test_boundary_length_is_accepted_when_valid(self):
        # A legit role is always short, so the cap never rejects valid input.
        assert _sanitize_role("admin") == "admin"

    def test_non_string_input_collapses_to_user(self):
        assert _sanitize_role(123) == "user"  # type: ignore[arg-type]
        assert _sanitize_role(["admin"]) == "user"  # type: ignore[arg-type]
