from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import IntrospectionPolicy

_BLOCKED_DUNDER: frozenset[str] = frozenset(
    {
        "__subclasses__",
        "__bases__",
        "__mro__",
        "__globals__",
        "__closure__",
        "__code__",
        "__dict__",
        "__class__",
    }
)


class AttributeGuard:
    def __init__(self, policy: IntrospectionPolicy, plugin_id: str | None = None) -> None:
        self._policy = policy
        self._plugin_id = plugin_id
        self._blocked = _BLOCKED_DUNDER | policy.blocked_attributes

    def safe_getattr(self, obj: Any, name: str, *default: Any) -> Any:
        if name in self._blocked:
            raise PermissionError(
                f"Attribute '{name}' is not accessible in strategy sandbox"
            )
        return getattr(obj, name, *default)

    def safe_setattr(self, obj: Any, name: str, value: Any) -> None:
        if name in self._blocked:
            raise PermissionError(
                f"Setting attribute '{name}' is not allowed in strategy sandbox"
            )
        setattr(obj, name, value)

    def is_blocked(self, name: str) -> bool:
        return name in self._blocked
