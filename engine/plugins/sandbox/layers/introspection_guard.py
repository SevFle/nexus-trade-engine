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
        "__reduce__",
        "__reduce_ex__",
        "__getstate__",
        "__setstate__",
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

_TRACEBACK_ATTRS: frozenset[str] = frozenset(
    {
        "__traceback__",
        "__context__",
        "__cause__",
        "tb_frame",
    }
)

_BLOCKED_BUILTINS_DEFAULT: frozenset[str] = frozenset(
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

_SAFE_DIR_ATTRS: frozenset[str] = _EXPLICITLY_BLOCKED_ATTRS | _FRAME_ATTRS | _TRACEBACK_ATTRS


class _RestrictedObject:
    @classmethod
    def __subclasses__(cls) -> list[type]:
        raise RuntimeError("__subclasses__() is not allowed in strategy sandbox")


def _make_safe_dir(original_dir: Any, guard: IntrospectionGuard) -> Any:
    def safe_dir(obj: Any = None) -> list[str]:
        result = original_dir(obj)
        return [
            attr
            for attr in result
            if attr not in _SAFE_DIR_ATTRS
            and attr not in guard._policy.blocked_attributes  # noqa: SLF001
        ]
    return safe_dir


class IntrospectionGuard:
    def __init__(self, policy: IntrospectionPolicy, plugin_id: str | None = None) -> None:
        self._policy = policy
        self._plugin_id = plugin_id
        self._original_getattr: Any = None
        self._original_setattr: Any = None
        self._original_object: Any = None
        self._original_builtins: dict[str, Any] = {}
        self._installed = False
        self._violation_log: list[IntrospectionViolation] = []

    def _is_blocked_attr(self, name: str) -> bool:
        if name in _EXPLICITLY_BLOCKED_ATTRS:
            return True
        if name in self._policy.blocked_attributes:
            return True
        if self._policy.block_frame_access:
            if name in _FRAME_ATTRS:
                return True
            if name in _TRACEBACK_ATTRS:
                return True
        return False

    def _restricted_getattr(self, obj: Any, name: str, *default: Any) -> Any:
        if self._is_blocked_attr(name):
            violation = IntrospectionViolation(name, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(violation.detail)
        return self._original_getattr(obj, name, *default)

    def _restricted_setattr(self, obj: Any, name: str, value: Any) -> None:
        if name in _FRAME_ATTRS or name in _TRACEBACK_ATTRS:
            violation = IntrospectionViolation(name, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(violation.detail)
        self._original_setattr(obj, name, value)

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

        all_blocked = self._policy.blocked_builtins | _BLOCKED_BUILTINS_DEFAULT

        for name in all_blocked:
            if name in builtins.__dict__:
                self._original_builtins[name] = builtins.__dict__[name]

        self._original_object = builtins.object
        builtins.object = _RestrictedObject

        self._original_getattr = builtins.getattr
        builtins.getattr = self._restricted_getattr

        self._original_setattr = builtins.setattr
        builtins.setattr = self._restricted_setattr

        for name in all_blocked:
            if name in self._original_builtins:
                builtins.__dict__[name] = self._make_blocked_builtin(name)

        if "dir" in builtins.__dict__:
            self._original_builtins["dir"] = builtins.__dict__["dir"]
            builtins.__dict__["dir"] = _make_safe_dir(builtins.__dict__["dir"], self)

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

        if self._original_setattr is not None:
            builtins.setattr = self._original_setattr
            self._original_setattr = None

        for name, original in self._original_builtins.items():
            setattr(builtins, name, original)
        self._original_builtins.clear()

        self._installed = False

    def get_violations(self) -> list[IntrospectionViolation]:
        return list(self._violation_log)

    def clear_violations(self) -> None:
        self._violation_log.clear()
