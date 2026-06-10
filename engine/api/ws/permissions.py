"""Permission matrix for WebSocket channel subscriptions (SEV-275)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PermissionCheck:
    required_scope: str
    all_scope: str
    owner_field: str | None = None


CHANNEL_PERMISSIONS: dict[str, PermissionCheck] = {
    "portfolio": PermissionCheck(
        required_scope="read:portfolio",
        all_scope="read:portfolio:all",
        owner_field="account_id",
    ),
    "orders": PermissionCheck(
        required_scope="read:orders",
        all_scope="read:orders:all",
        owner_field="account_id",
    ),
    "strategies": PermissionCheck(
        required_scope="read:strategies",
        all_scope="read:strategies:all",
        owner_field="strategy_id",
    ),
}


def check_channel_access(
    channel: str,
    scopes: list[str],
    params: dict,
    user_id: str | None = None,
) -> tuple[bool, str | None]:
    """Check if a user with given scopes can access a channel.

    Returns (allowed, error_code). error_code is None on success.

    Owner-based access: when the user has the base scope but not the
    :all scope, the owner_field value in params must match the
    authenticated user_id.
    """
    perm = CHANNEL_PERMISSIONS.get(channel)
    if perm is None:
        return False, "404"
    if perm.all_scope in scopes:
        return True, None
    if perm.required_scope in scopes:
        if (
            perm.owner_field
            and perm.owner_field in params
            and user_id is not None
            and params[perm.owner_field] != user_id
        ):
            return False, "403"
        return True, None
    return False, "403"


_ROOM_PARAM_MAP: dict[str, list[tuple[str, str]]] = {
    "portfolio": [
        ("account_id", "portfolio:account"),
        ("strategy_id", "portfolio:strategy"),
    ],
    "orders": [
        ("symbol", "orders:symbol"),
        ("status", "orders:status"),
    ],
    "strategies": [
        ("strategy_id", "strategies:strategy"),
    ],
}


def resolve_room_name(channel: str, params: dict) -> str | None:
    """Resolve channel + params into a deterministic room name.

    Returns None if required params are missing.
    """
    builders = _ROOM_PARAM_MAP.get(channel)
    if builders is None:
        return None
    for param_name, prefix in builders:
        value = params.get(param_name)
        if value:
            return f"{prefix}:{value}"
    return None
