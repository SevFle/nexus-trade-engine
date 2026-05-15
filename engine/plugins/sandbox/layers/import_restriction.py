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
        self._original_importlib_import_module: Any = None

    def _is_module_blocked(self, name: str) -> bool:
        root = name.split(".", maxsplit=1)[0]
        if root in self.blocked:
            return True
        return self.allowed is not None and root not in self.allowed

    def find_spec(
        self,
        fullname: str,
        _path: object = None,
        _target: object = None,
    ) -> ModuleSpec | None:
        if self._is_module_blocked(fullname):
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
        if level == 0 and self._is_module_blocked(name):
            violation = ImportViolation(name, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise ImportError(violation.detail)
        return self._original_import(name, globals_, locals_, fromlist, level)

    def _restricted_importlib_import_module(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self._is_module_blocked(name):
            violation = ImportViolation(name, plugin_id=self._plugin_id)
            self._violation_log.append(violation)
            raise ImportError(violation.detail)
        return self._original_importlib_import_module(name, *args, **kwargs)

    def install(self) -> None:
        if not self._installed:
            self._original_import = builtins.__import__
            builtins.__import__ = self._restricted_import
            sys.meta_path.insert(0, self)
            if "importlib" in sys.modules:
                importlib_mod = sys.modules["importlib"]
                if hasattr(importlib_mod, "import_module"):
                    self._original_importlib_import_module = importlib_mod.import_module
                    importlib_mod.import_module = self._restricted_importlib_import_module
            self._installed = True

    def uninstall(self) -> None:
        if self._installed:
            builtins.__import__ = self._original_import
            if self in sys.meta_path:
                sys.meta_path.remove(self)
            if self._original_importlib_import_module is not None:
                if "importlib" in sys.modules:
                    sys.modules["importlib"].import_module = self._original_importlib_import_module
                self._original_importlib_import_module = None
            self._installed = False

    def get_violations(self) -> list[ImportViolation]:
        return list(self._violation_log)

    def clear_violations(self) -> None:
        self._violation_log.clear()
