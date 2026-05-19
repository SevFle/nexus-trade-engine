from __future__ import annotations

import builtins
import sys
from importlib.abc import MetaPathFinder
from typing import TYPE_CHECKING, Any

from engine.plugins.sandbox.core.violation import ImportViolation

if TYPE_CHECKING:
    from collections.abc import Callable
    from importlib.machinery import ModuleSpec

from engine.plugins.restricted_importer import BLOCKED_MODULES


class RestrictedImporter(MetaPathFinder):
    def __init__(
        self,
        blocked: set[str] | None = None,
        allowed: set[str] | None = None,
        plugin_id: str | None = None,
    ) -> None:
        self.blocked = blocked or set(BLOCKED_MODULES)
        self.allowed = allowed
        self._installed = False
        self._original_import: Callable[..., Any] = builtins.__import__
        self._plugin_id = plugin_id
        self._violation_log: list[ImportViolation] = []

    def find_spec(
        self,
        fullname: str,
        _path: object = None,
        _target: object = None,
    ) -> ModuleSpec | None:
        root = fullname.split(".", maxsplit=1)[0]
        if root in self.blocked:
            violation = ImportViolation(fullname, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise ImportError(violation.detail)
        if self.allowed is not None and root not in self.allowed:
            violation = ImportViolation(fullname, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise ImportError(violation.detail)
        return None

    def _restricted_import(
        self,
        name: str,
        globals_: object = None,
        locals_: object = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if level == 0:
            root = name.split(".", maxsplit=1)[0]
            if root in self.blocked:
                violation = ImportViolation(name, plugin_id=self._plugin_id)
                self._violation_log.append(violation)
                raise ImportError(violation.detail)
            if self.allowed is not None and root not in self.allowed:
                violation = ImportViolation(name, plugin_id=self._plugin_id)
                self._violation_log.append(violation)
                raise ImportError(violation.detail)
        return self._original_import(name, globals_, locals_, fromlist, level)

    def install(self) -> None:
        if not self._installed:
            self._original_import = builtins.__import__
            builtins.__import__ = self._restricted_import
            sys.meta_path.insert(0, self)
            self._installed = True

    def uninstall(self) -> None:
        if self._installed:
            builtins.__import__ = self._original_import
            if self in sys.meta_path:
                sys.meta_path.remove(self)
            self._installed = False

    def get_violations(self) -> list[ImportViolation]:
        return list(self._violation_log)

    def clear_violations(self) -> None:
        self._violation_log.clear()
