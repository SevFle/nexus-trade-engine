from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import IntrospectionPolicy

from engine.plugins.sandbox.core.violation import IntrospectionViolation

_BLOCKED_ATTRS: frozenset[str] = frozenset(
    {
        "__subclasses__",
        "__bases__",
        "__mro__",
        "__globals__",
        "__closure__",
        "__code__",
        "__dict__",
    }
)

_EXPLICITLY_BLOCKED_ATTRS: frozenset[str] = frozenset(
    {
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
        "f_back",
        "f_builtins",
        "f_code",
        "f_globals",
        "f_locals",
        "__traceback__",
        "__context__",
        "__cause__",
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
        self._original_setattr: Any = None
        self._original_dir: Any = None
        self._original_object: Any = None
        self._original_builtins: dict[str, Any] = {}
        self._installed = False
        self._violation_log: list[IntrospectionViolation] = []

    def _is_blocked_attr(self, name: str) -> bool:
        if name in _BLOCKED_ATTRS or name in self._policy.blocked_attributes:
            return True
        if name in _EXPLICITLY_BLOCKED_ATTRS:
            return True
        if self._policy.block_frame_access and name in _FRAME_ATTRS:
            return True
        return False

    def _restricted_getattr(self, obj: Any, name: str, *default: Any) -> Any:
        if self._is_blocked_attr(name):
            violation = IntrospectionViolation(name, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(violation.detail)
        return self._original_getattr(obj, name, *default)

    def _restricted_setattr(self, obj: Any, name: str, value: Any) -> None:
        if self._is_blocked_attr(name):
            violation = IntrospectionViolation(name, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(violation.detail)
        return self._original_setattr(obj, name, value)

    def _restricted_dir(self, obj: Any = None) -> list[str]:
        result = self._original_dir() if obj is None else self._original_dir(obj)
        blocked = (
            _BLOCKED_ATTRS
            | _EXPLICITLY_BLOCKED_ATTRS
            | _FRAME_ATTRS
            | self._policy.blocked_attributes
        )
        return [name for name in result if name not in blocked]

    def _make_restricted_builtin(self, name: str) -> Any:
        def _blocked(*_args: Any, **_kwargs: Any) -> Any:
            violation = IntrospectionViolation(name, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(f"builtin '{name}' is not available in strategy sandbox")
        return _blocked

    def install(self) -> None:
        if self._installed:
            return

        self._original_object = builtins.object
        builtins.object = _RestrictedObject

        self._original_getattr = builtins.getattr
        builtins.getattr = self._restricted_getattr

        self._original_setattr = builtins.setattr
        builtins.setattr = self._restricted_setattr

        self._original_dir = builtins.dir
        builtins.dir = self._restricted_dir

        for name in self._policy.blocked_builtins:
            if hasattr(builtins, name):
                self._original_builtins[name] = getattr(builtins, name)
                setattr(builtins, name, self._make_restricted_builtin(name))

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

        if self._original_dir is not None:
            builtins.dir = self._original_dir
            self._original_dir = None

        for name, original in self._original_builtins.items():
            setattr(builtins, name, original)
        self._original_builtins.clear()

        self._installed = False

    def get_violations(self) -> list[IntrospectionViolation]:
        return list(self._violation_log)

    def clear_violations(self) -> None:
        self._violation_log.clear()
