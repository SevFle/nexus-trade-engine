"""Tests for engine.auth.rbac — RBAC role hierarchy, permissions, and scope resolution."""

from __future__ import annotations

import pytest

from engine.auth.rbac import (
    ROLE_ALIASES,
    ROLE_PERMISSIONS,
    ROLES,
    Permission,
    get_permissions_for_role,
    resolve_role_from_permissions,
    role_has_permission,
    role_meets_minimum,
)


class TestRoleConstants:
    def test_three_default_roles(self):
        assert set(ROLES.keys()) == {"viewer", "trader", "admin"}

    def test_role_levels(self):
        assert ROLES["viewer"].level == 0
        assert ROLES["trader"].level == 1
        assert ROLES["admin"].level == 2

    def test_legacy_aliases(self):
        assert ROLE_ALIASES["user"] == "viewer"
        assert ROLE_ALIASES["developer"] == "trader"


class TestPermissionConstants:
    def test_four_permissions(self):
        assert set(Permission) == {"read", "write", "trade", "admin"}

    def test_permission_is_str_enum(self):
        assert Permission.READ == "read"
        assert Permission.WRITE == "write"
        assert Permission.TRADE == "trade"
        assert Permission.ADMIN == "admin"


class TestRolePermissions:
    def test_viewer_read_only(self):
        assert ROLE_PERMISSIONS["viewer"] == frozenset({Permission.READ})

    def test_trader_read_write_trade(self):
        assert ROLE_PERMISSIONS["trader"] == frozenset({
            Permission.READ, Permission.WRITE, Permission.TRADE,
        })

    def test_admin_all_permissions(self):
        assert ROLE_PERMISSIONS["admin"] == frozenset({
            Permission.READ, Permission.WRITE, Permission.TRADE, Permission.ADMIN,
        })

    def test_every_role_has_read(self):
        for role_perms in ROLE_PERMISSIONS.values():
            assert Permission.READ in role_perms


class TestGetPermissionsForRole:
    def test_viewer_permissions(self):
        perms = get_permissions_for_role("viewer")
        assert Permission.READ in perms
        assert Permission.WRITE not in perms
        assert Permission.TRADE not in perms
        assert Permission.ADMIN not in perms

    def test_trader_permissions(self):
        perms = get_permissions_for_role("trader")
        assert Permission.READ in perms
        assert Permission.WRITE in perms
        assert Permission.TRADE in perms
        assert Permission.ADMIN not in perms

    def test_admin_permissions(self):
        perms = get_permissions_for_role("admin")
        assert len(perms) == 4

    def test_legacy_user_alias(self):
        assert get_permissions_for_role("user") == get_permissions_for_role("viewer")

    def test_legacy_developer_alias(self):
        assert get_permissions_for_role("developer") == get_permissions_for_role("trader")

    def test_unknown_role_returns_empty(self):
        assert get_permissions_for_role("nonexistent") == frozenset()


class TestRoleHasPermission:
    @pytest.mark.parametrize("role", ["viewer", "user"])
    def test_viewer_has_read(self, role):
        assert role_has_permission(role, "read") is True

    @pytest.mark.parametrize("role", ["viewer", "user"])
    def test_viewer_no_write(self, role):
        assert role_has_permission(role, "write") is False

    @pytest.mark.parametrize("role", ["trader", "developer"])
    def test_trader_has_trade(self, role):
        assert role_has_permission(role, "trade") is True

    @pytest.mark.parametrize("role", ["trader", "developer"])
    def test_trader_no_admin(self, role):
        assert role_has_permission(role, "admin") is False

    def test_admin_has_all(self):
        for perm in Permission:
            assert role_has_permission("admin", perm) is True

    def test_admin_has_admin(self):
        assert role_has_permission("admin", Permission.ADMIN) is True

    def test_unknown_role_no_permission(self):
        assert role_has_permission("nonexistent", "read") is False

    def test_permission_string_or_enum(self):
        assert role_has_permission("viewer", "read") is True
        assert role_has_permission("viewer", Permission.READ) is True


class TestRoleMeetsMinimum:
    def test_viewer_meets_viewer(self):
        assert role_meets_minimum("viewer", "viewer") is True

    def test_viewer_not_meets_trader(self):
        assert role_meets_minimum("viewer", "trader") is False

    def test_trader_meets_viewer(self):
        assert role_meets_minimum("trader", "viewer") is True

    def test_admin_meets_all(self):
        for role in ["viewer", "trader", "admin"]:
            assert role_meets_minimum("admin", role) is True

    def test_legacy_user_meets_viewer(self):
        assert role_meets_minimum("user", "viewer") is True

    def test_legacy_developer_meets_trader(self):
        assert role_meets_minimum("developer", "trader") is True

    def test_viewer_not_meets_admin(self):
        assert role_meets_minimum("viewer", "admin") is False


class TestResolveRoleFromPermissions:
    def test_empty_permissions_viewer(self):
        assert resolve_role_from_permissions([]) == "viewer"

    def test_read_only_viewer(self):
        assert resolve_role_from_permissions(["read"]) == "viewer"

    def test_write_gives_trader(self):
        assert resolve_role_from_permissions(["read", "write"]) == "trader"

    def test_trade_gives_trader(self):
        assert resolve_role_from_permissions(["read", "trade"]) == "trader"

    def test_admin_gives_admin(self):
        assert resolve_role_from_permissions(["admin"]) == "admin"

    def test_all_permissions_admin(self):
        assert resolve_role_from_permissions(["read", "write", "trade", "admin"]) == "admin"


class TestHierarchyTransitivity:
    def test_viewer_lt_trader_lt_admin(self):
        assert ROLES["viewer"].level < ROLES["trader"].level
        assert ROLES["trader"].level < ROLES["admin"].level

    def test_permissions_inherit(self):
        viewer_perms = get_permissions_for_role("viewer")
        trader_perms = get_permissions_for_role("trader")
        admin_perms = get_permissions_for_role("admin")
        assert viewer_perms.issubset(trader_perms)
        assert trader_perms.issubset(admin_perms)
