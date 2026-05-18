from __future__ import annotations

import atexit
import builtins
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.plugins.sandbox.core.policy import IntrospectionPolicy

from engine.plugins.sandbox.core.violation import IntrospectionViolation


def _make_tls_flag():
    _tls = threading.local()

    def get() -> bool:
        try:
            return _tls.v
        except AttributeError:
            return False

    def set_flag(v: bool) -> None:
        _tls.v = v

    return get, set_flag


_is_uninstalling, _set_uninstalling = _make_tls_flag()

_original_object_class_ref: Any = None

_BUILTIN_GETATTR = builtins.getattr
_BUILTIN_SETATTR = builtins.setattr

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
        "__builtins__",
        "__func__",
        "__self__",
        "__module__",
        "__weakref__",
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
        if _is_uninstalling():
            if _original_object_class_ref is not None:
                return _original_object_class_ref.__subclasses__()
            return []
        raise RuntimeError("__subclasses__() is not allowed in strategy sandbox")


def _make_safe_dir(original_dir: Any, guard: IntrospectionGuard) -> Any:
    def safe_dir(obj: Any = None) -> list[str]:
        if _is_uninstalling() or not guard._installed:  # noqa: SLF001
            return original_dir(obj)
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
        if not self._installed or _is_uninstalling():
            getter = self._original_getattr
            if getter is not None:
                return getter(obj, name, *default)
            return _BUILTIN_GETATTR(obj, name, *default)
        if self._is_blocked_attr(name):
            violation = IntrospectionViolation(name, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(violation.detail)
        return self._original_getattr(obj, name, *default)

    def _restricted_setattr(self, obj: Any, name: str, value: Any) -> None:
        if not self._installed or _is_uninstalling():
            setter = self._original_setattr
            if setter is not None:
                return setter(obj, name, value)
            return _BUILTIN_SETATTR(obj, name, value)
        if name in _FRAME_ATTRS or name in _TRACEBACK_ATTRS:
            violation = IntrospectionViolation(name, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise PermissionError(violation.detail)
        return self._original_setattr(obj, name, value)

    def _make_blocked_builtin(self, name: str) -> Any:
        guard = self
        original = self._original_builtins.get(name)

        def _blocked(*args: Any, **kwargs: Any) -> Any:
            if _is_uninstalling() or not guard._installed:
                if original is not None:
                    return original(*args, **kwargs)
                return None
            violation = IntrospectionViolation(name, plugin_id=guard._plugin_id)
            guard._violation_log.append(violation)
            raise PermissionError(violation.detail)

        return _blocked

    def install(self) -> None:
        global _original_object_class_ref  # noqa: PLW0603

        if self._installed:
            return

        all_blocked = self._policy.blocked_builtins | _BLOCKED_BUILTINS_DEFAULT

        for name in all_blocked:
            if name in builtins.__dict__:
                self._original_builtins[name] = builtins.__dict__[name]

        self._original_object = builtins.object
        _original_object_class_ref = builtins.object
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
        atexit.register(self._atexit_cleanup)

    def _atexit_cleanup(self) -> None:
        try:
            if self._installed:
                self.uninstall()
        except Exception:  # noqa: S110
            pass

    def uninstall(self) -> None:
        global _original_object_class_ref  # noqa: PLW0603

        if not self._installed:
            return

        _set_uninstalling(True)
        try:
            if self._original_object is not None:
                builtins.object = self._original_object
                self._original_object = None
                _original_object_class_ref = None

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
        finally:
            _set_uninstalling(False)
            atexit.unregister(self._atexit_cleanup)

    def get_violations(self) -> list[IntrospectionViolation]:
        return list(self._violation_log)

    def clear_violations(self) -> None:
        self._violation_log.clear()
