from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import IntrospectionPolicy

_BLOCKED_BUILTINS: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "breakpoint",
        "vars",
        "globals",
        "locals",
    }
)


def build_safe_builtins(
    policy: IntrospectionPolicy,
    plugin_id: str | None = None,
) -> dict[str, Any]:
    safe = dict(builtins.__dict__)
    all_blocked = policy.blocked_builtins | _BLOCKED_BUILTINS

    for name in all_blocked:
        if name in safe:
            safe[name] = _make_blocked_builtin(name, plugin_id)

    return safe


def build_restricted_globals(
    policy: IntrospectionPolicy,
    plugin_id: str | None = None,
) -> dict[str, Any]:
    safe_builtins = build_safe_builtins(policy, plugin_id)
    return {"__builtins__": safe_builtins}


def _make_blocked_builtin(name: str, _plugin_id: str | None = None) -> Any:
    def _blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError(
            f"Builtin '{name}' is not accessible in strategy sandbox"
        )
    return _blocked
