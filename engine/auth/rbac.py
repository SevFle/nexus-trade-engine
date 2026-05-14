"""RBAC role hierarchy and permission resolution (ADR-0002).

Implements the resource x action grid specified in ADR-0002 with
three default roles and four permission categories.  The module
also supports the legacy ``user`` / ``developer`` role names via
alias mapping so existing deployments continue to work.

Role taxonomy (ADR-0002):
    viewer — read-only access
    trader — read + write + live trading (own portfolios)
    admin  — all permissions including admin operations

Legacy compatibility:
    user      → viewer
    developer → trader

Permission model:
    read  — GET / HEAD requests
    write — POST / PUT / PATCH for backtest, portfolio, webhooks, etc.
    trade — submit orders, live trading routes
    admin — system configuration, user management, all portfolios
"""

from __future__ import annotations

from enum import StrEnum
from typing import NamedTuple


class Role(NamedTuple):
    name: str
    display_name: str
    level: int


class Permission(StrEnum):
    READ = "read"
    WRITE = "write"
    TRADE = "trade"
    ADMIN = "admin"


ROLES: dict[str, Role] = {
    "viewer": Role(name="viewer", display_name="Viewer", level=0),
    "trader": Role(name="trader", display_name="Trader", level=1),
    "admin": Role(name="admin", display_name="Administrator", level=2),
}

ROLE_ALIASES: dict[str, str] = {
    "user": "viewer",
    "developer": "trader",
}

ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "viewer": frozenset({Permission.READ}),
    "trader": frozenset({Permission.READ, Permission.WRITE, Permission.TRADE}),
    "admin": frozenset({Permission.READ, Permission.WRITE, Permission.TRADE, Permission.ADMIN}),
}

PERMISSIONS = Permission

PERMISSION_DESCRIPTIONS: dict[Permission, str] = {
    Permission.READ: "Read-only access to resources",
    Permission.WRITE: "Create and modify resources",
    Permission.TRADE: "Submit orders and manage live trading",
    Permission.ADMIN: "System administration and user management",
}

_SCOPE_HIERARCHY: dict[Permission, int] = {
    Permission.READ: 0,
    Permission.WRITE: 1,
    Permission.TRADE: 1,
    Permission.ADMIN: 2,
}


def _canonicalise_role(role: str) -> str:
    if role in ROLES:
        return role
    return ROLE_ALIASES.get(role, role)


def get_permissions_for_role(role: str) -> frozenset[Permission]:
    canonical = _canonicalise_role(role)
    return ROLE_PERMISSIONS.get(canonical, frozenset())


def role_has_permission(role: str, permission: str | Permission) -> bool:
    perms = get_permissions_for_role(role)
    target = Permission(permission)
    if target in perms:
        return True
    required_level = _SCOPE_HIERARCHY.get(target, -1)
    return any(
        _SCOPE_HIERARCHY.get(p, -1) >= required_level
        for p in perms
    )


def role_meets_minimum(role: str, minimum_role: str) -> bool:
    canonical = _canonicalise_role(role)
    min_canonical = _canonicalise_role(minimum_role)
    role_level = ROLES.get(canonical, ROLES["viewer"]).level
    min_level = ROLES.get(min_canonical, ROLES["viewer"]).level
    return role_level >= min_level


def resolve_role_from_permissions(permissions: list[str]) -> str:
    if not permissions:
        return "viewer"
    if Permission.ADMIN in permissions or "admin" in permissions:
        return "admin"
    has_elevated = any(
        p in (Permission.TRADE, Permission.WRITE, "trade", "write") for p in permissions
    )
    if has_elevated:
        return "trader"
    return "viewer"
