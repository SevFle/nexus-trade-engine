"""Static sandboxing helpers for strategy plugins.

This package hosts *pre-execution* security gates that inspect strategy
source before it is ever compiled or run — complementing the in-process
runtime enforcement in :mod:`engine.plugins.restricted_importer` and
:mod:`engine.plugins.sandbox`.

Public API
----------
* :class:`ImportChecker` — AST visitor that flags blocked imports.
* :func:`validate_source` — parse + walk + raise convenience entry point.
* :class:`SecurityViolation` — exception raised on a blocked import.
* :class:`SandboxConfig` — configurable blocklist / override allowlist.
"""

from __future__ import annotations

from engine.sandbox.import_validator import (
    DEFAULT_BLOCKED_IMPORTS,
    ImportChecker,
    SandboxConfig,
    SecurityViolation,
    validate_source,
)

__all__ = [
    "DEFAULT_BLOCKED_IMPORTS",
    "ImportChecker",
    "SandboxConfig",
    "SecurityViolation",
    "validate_source",
]
