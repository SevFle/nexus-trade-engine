from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import IntrospectionPolicy

from engine.plugins.sandbox.core.violation import IntrospectionViolation

_EXPLICITLY_BLOCKED_ATTRS: frozenset[str] = frozenset(
    {
        "__subclasses__",
        "__bases__",
        "__mro__",
        "__globals__",
        "__closure__",
        "__code__",
        "__dict__",
        "__class__",
        "__init_subclass__",
        "__instancecheck__",
        "__subclasscheck__",
    }
)

_FRAME_ATTRS: frozenset[str] = frozenset(
    {
        "tb_frame",
        "tb_lineno",
        "tb_next",
        "f_back",
        "f_builtins",
        "f_code",
        "f_globals",
        "f_locals",
        "f_trace",
    }
)

_BLOCKED_BUILTINS_DEFAULT: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "breakpoint",
    }
)


class _RestrictedObject:
    @classmethod
    def __subclasses__(cls) -> list[type]:
        raise RuntimeError("__subclasses__() is not allowed in strategy sandbox")


class IntrospectionGuard:
    def __init__(self, policy: IntrospectionPolicy, plugin_id: str | None = None) -> None:
        self._policy = policy
        self._plugin_id = plugin_id
        self._original_getattr: Any = None
        self._original_object: Any = None
        self._original_builtins: dict[str, Any] = {}
        self._installed = False
        self._violation_log: list[IntrospectionViolation] = []

    def _is_blocked_attr(self, name: str) -> bool:
        if name in _EXPLICITLY_BLOCKED_ATTRS:
            return True
        if name in self._policy.blocked_attributes:
            return True
        return name in _FRAME_ATTRS

    def _restricted_getattr(self, obj: Any, name: str, *default: Any) -> Any:
        if self._is_blocked_attr(name):
            violation = IntrospectionViolation(name, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(violation.detail)
        return self._original_getattr(obj, name, *default)

    def _make_blocked_builtin(self, name: str) -> Any:
        guard = self

        def _blocked(*_args: Any, **_kwargs: Any) -> Any:
            violation = IntrospectionViolation(name, plugin_id=guard._plugin_id)
            guard._violation_log.append(violation)
            raise PermissionError(violation.detail)

        return _blocked

    def install(self) -> None:
        if self._installed:
            return

        self._original_object = builtins.object
        builtins.object = _RestrictedObject

        self._original_getattr = builtins.getattr
        builtins.getattr = self._restricted_getattr

        all_blocked = self._policy.blocked_builtins | _BLOCKED_BUILTINS_DEFAULT
        for name in all_blocked:
            if hasattr(builtins, name):
                self._original_builtins[name] = getattr(builtins, name)
                setattr(builtins, name, self._make_blocked_builtin(name))

        self._installed = True

    def uninstall(self) -> None:
        if not self._installed:
            return

        if self._original_object is not None:
            builtins.object = self._original_object
            self._original_object = None

        if self._original_getattr is not None:
            builtins.getattr = self._original_getattr
            self._original_getattr = None

        for name, original in self._original_builtins.items():
            setattr(builtins, name, original)
        self._original_builtins.clear()

        self._installed = False

    def get_violations(self) -> list[IntrospectionViolation]:
        return list(self._violation_log)

    def clear_violations(self) -> None:
        self._violation_log.clear()
