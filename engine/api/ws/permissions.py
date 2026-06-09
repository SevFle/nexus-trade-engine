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
        owner_field="symbol",
    ),
    "strategies": PermissionCheck(
        required_scope="read:strategies",
        all_scope="read:strategies:all",
        owner_field="strategy_id",
    ),
}


def check_channel_access(
    channel: str, scopes: list[str], params: dict
) -> tuple[bool, str | None]:
    """Check if a user with given scopes can access a channel.

    Returns (allowed, error_code). error_code is None on success.
    """
    perm = CHANNEL_PERMISSIONS.get(channel)
    if perm is None:
        return False, "404"
    if perm.all_scope in scopes:
        return True, None
    if perm.required_scope in scopes:
        return True, None
    return False, "403"


def resolve_room_name(channel: str, params: dict) -> str | None:
    """Resolve channel + params into a deterministic room name.

    Returns None if required params are missing.
    """
    if channel == "portfolio":
        account_id = params.get("account_id")
        if account_id:
            return f"portfolio:account:{account_id}"
        strategy_id = params.get("strategy_id")
        if strategy_id:
            return f"portfolio:strategy:{strategy_id}"
        return None
    if channel == "orders":
        symbol = params.get("symbol")
        if symbol:
            return f"orders:symbol:{symbol}"
        status = params.get("status")
        if status:
            return f"orders:status:{status}"
        return None
    if channel == "strategies":
        strategy_id = params.get("strategy_id")
        if strategy_id:
            return f"strategies:strategy:{strategy_id}"
        return None
    return None
