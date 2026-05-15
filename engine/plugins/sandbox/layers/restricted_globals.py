from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import IntrospectionPolicy

from engine.plugins.sandbox.layers.safe_builtins import build_restricted_globals


def create_restricted_globals(
    policy: IntrospectionPolicy,
    plugin_id: str | None = None,
) -> dict[str, Any]:
    return build_restricted_globals(policy, plugin_id)
