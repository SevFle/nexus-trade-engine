"""
Layer 1: Import restriction system for the strategy sandbox.

Blocks dangerous module imports to prevent strategies from accessing
the filesystem, network (beyond declared endpoints), or system resources.

Uses both a meta-path finder (for new imports) and a builtins.__import__
override (to catch re-imports of already-cached modules like os, sys).
"""

from __future__ import annotations

import builtins
import sys
from importlib.abc import MetaPathFinder
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from importlib.machinery import ModuleSpec

BLOCKED_MODULES: frozenset[str] = frozenset(
    [
        "os",
        "subprocess",
        "shutil",
        "pathlib",
        "socket",
        "http",
        "urllib",
        "ctypes",
        "multiprocessing",
        "signal",
        "sys",
        "importlib",
        "io",
        "_io",
    ]
)


class RestrictedImporter(MetaPathFinder):
    """
    Import hook that blocks access to dangerous modules.

    Dual-layer enforcement:
      1. ``sys.meta_path`` finder - catches imports of modules not yet loaded.
      2. ``builtins.__import__`` override - catches re-imports of cached modules.
    """

    def __init__(self, blocked: set[str] | None = None) -> None:
        self.blocked = blocked or set(BLOCKED_MODULES)
        self._installed = False
        self._original_import: Callable[..., Any] = builtins.__import__

    def find_spec(
        self,
        fullname: str,
        _path: object = None,
        _target: object = None,
    ) -> ModuleSpec | None:
        root = fullname.split(".", maxsplit=1)[0]
        if root in self.blocked:
            raise ImportError(f"Module '{fullname}' is blocked in strategy sandbox")
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
                raise ImportError(f"Module '{name}' is blocked in strategy sandbox")
        return self._original_import(name, globals_, locals_, fromlist, level)

    def install(self) -> None:
        if not self._installed:
            self._original_import = builtins.__import__
            builtins.__import__ = self._restricted_import  # type: ignore[assignment]
            sys.meta_path.insert(0, self)
            self._installed = True

    def uninstall(self) -> None:
        if self._installed:
            builtins.__import__ = self._original_import  # type: ignore[assignment]
            if self in sys.meta_path:
                sys.meta_path.remove(self)
            self._installed = False
