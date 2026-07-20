"""Re-export facade for engine.tasks.worker.

Importing the *package-level* names from :mod:`engine.tasks` — e.g.
``from engine.tasks import broker`` or ``engine.tasks.scheduler`` — is
deprecated; import from :mod:`engine.tasks.worker` (or, for the broker
primitives, the canonical :mod:`engine.tasks.broker`) instead.

Importing **submodules** (``engine.tasks.broker``, ``engine.tasks.worker``,
``engine.tasks.definitions``) is *not* deprecated and must stay silent:
the FastAPI app factory imports :mod:`engine.tasks.broker` to wire the
broker lifecycle into its lifespan, and several other modules import the
submodules directly. To avoid a spurious deprecation warning on every one
of those imports, the warning is deferred to actual facade attribute
access via PEP 562 module-level ``__getattr__`` rather than emitted at
package import time.
"""

from __future__ import annotations

import warnings
from typing import Any

# Names that the historical facade re-exported from ``engine.tasks.worker``.
# Accessing any of them on the package object triggers the deprecation
# warning and a lazy import+attribute lookup on the real ``worker`` module.
_DEPRECATED_FACADE_NAMES: frozenset[str] = frozenset(
    {"broker", "run_backtest_task", "scheduler"}
)


def __getattr__(name: str) -> Any:
    """Lazy, deprecating access to the historical facade names.

    Only fires for the re-exported facade attributes (``broker``,
    ``run_backtest_task``, ``scheduler``). Submodule imports such as
    ``from engine.tasks.broker import broker`` resolve through the normal
    import machinery and never reach here, so they stay warning-free.

    :raises AttributeError: for any name that is neither a facade attribute
        nor resolvable as a submodule of :mod:`engine.tasks`.
    """
    if name in _DEPRECATED_FACADE_NAMES:
        warnings.warn(
            "Importing from 'engine.tasks' is deprecated; import from "
            "'engine.tasks.worker' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        from engine.tasks import worker

        try:
            return getattr(worker, name)
        except AttributeError as exc:  # pragma: no cover - defensive
            raise AttributeError(
                f"module 'engine.tasks' has no attribute {name!r}"
            ) from exc
    raise AttributeError(f"module 'engine.tasks' has no attribute {name!r}")


def __dir__() -> list[str]:
    """Advertise the facade surface for ``dir(engine.tasks)`` / REPL use."""
    return sorted(_DEPRECATED_FACADE_NAMES)
