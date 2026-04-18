"""
Layer 1: Import Restrictions - blocks dangerous stdlib modules in strategy sandbox.

Hooks into sys.meta_path to intercept imports before they resolve.
Blocked modules cover filesystem, networking, low-level system access.
sys and importlib are excluded because they are required by Python's
internal import machinery; strategies that use importlib.import_module()
still hit the RestrictedImporter for blocked targets.

Production note (Layer 5):
    In production, each strategy runs in an isolated subprocess/container.
    This in-process import hook is the MVP isolation layer.
"""

from __future__ import annotations

import contextlib
import sys
from importlib.abc import MetaPathFinder
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from importlib.machinery import ModuleSpec
    from types import ModuleType

BLOCKED_MODULES = frozenset(
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
    ]
)


class RestrictedImporter(MetaPathFinder):
    """Meta-path finder that blocks imports of dangerous modules."""

    def find_spec(  # type: ignore[override]
        self,
        fullname: str,
        path: Sequence[str | bytes] | None = None,  # noqa: ARG002
        target: ModuleType | None = None,  # noqa: ARG002
    ) -> ModuleSpec | None:
        root = fullname.split(".", maxsplit=1)[0]
        if root in BLOCKED_MODULES:
            raise ImportError(f"Module '{fullname}' is blocked in strategy sandbox")
        return None

    def install(self) -> None:
        if self not in sys.meta_path:
            sys.meta_path.insert(0, self)

    def uninstall(self) -> None:
        with contextlib.suppress(ValueError):
            sys.meta_path.remove(self)
