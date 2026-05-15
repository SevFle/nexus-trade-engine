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
        "credits",
        "license",
        "quit",
        "exit",
        "__import__",
        "help",
    }
)


class _RestrictedObject:
    @classmethod
    def __subclasses__(cls) -> list[type]:
        raise RuntimeError("__subclasses__() is not allowed in strategy sandbox")


def _make_blocked_builtin(name: str) -> Any:
    def _blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError(f"builtin '{name}' is not available in strategy sandbox")

    return _blocked


class IntrospectionGuard:
    def __init__(self, policy: IntrospectionPolicy, plugin_id: str | None = None) -> None:
        self._policy = policy
        self._plugin_id = plugin_id
        self._original_getattr: Any = None
        self._original_object: Any = None
        self._original_builtins: dict[str, Any] = {}
        self._installed = False
        self._violation_log: list[IntrospectionViolation] = []

    _SAFE_DUNDERS: frozenset[str] = frozenset(
        {
            "__init__",
            "__repr__",
            "__str__",
            "__len__",
            "__eq__",
            "__hash__",
            "__iter__",
            "__next__",
            "__getitem__",
            "__setitem__",
            "__delitem__",
            "__contains__",
            "__bool__",
            "__int__",
            "__float__",
            "__complex__",
            "__add__",
            "__radd__",
            "__sub__",
            "__rsub__",
            "__mul__",
            "__rmul__",
            "__truediv__",
            "__floordiv__",
            "__mod__",
            "__neg__",
            "__pos__",
            "__abs__",
            "__call__",
            "__enter__",
            "__exit__",
            "__name__",
            "__doc__",
            "__module__",
            "__new__",
            "__lt__",
            "__le__",
            "__gt__",
            "__ge__",
            "__ne__",
            "__getattr__",
            "__setattr__",
            "__get__",
            "__set__",
            "__notes__",
            "__cause__",
            "__context__",
            "__suppress_context__",
            "__traceback__",
            "__weakref__",
            "__slots__",
            "__all__",
            "__file__",
            "__path__",
            "__package__",
            "__spec__",
            "__loader__",
            "__builtins__",
            "__qualname__",
            "__annotations__",
            "__type_params__",
            "__orig_bases__",
            "__args__",
            "__parameters__",
            "__origin__",
        }
    )

    def _is_blocked_attr(self, name: str) -> bool:
        if name in _BLOCKED_ATTRS or name in self._policy.blocked_attributes:
            return True
        if (
            self._policy.blocked_dunder_access
            and name.startswith("__")
            and name.endswith("__")
            and name not in self._SAFE_DUNDERS
        ):
            return True
        return name in _FRAME_ATTRS

    def _restricted_getattr(self, obj: Any, name: str, *default: Any) -> Any:
        if self._is_blocked_attr(name):
            violation = IntrospectionViolation(name, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(violation.detail)
        return self._original_getattr(obj, name, *default)

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
                setattr(builtins, name, _make_blocked_builtin(name))

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
