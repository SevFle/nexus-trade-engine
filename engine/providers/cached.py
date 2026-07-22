"""Backwards-compatible re-export of :mod:`engine.data.providers.cached`.

This module exists for historical reasons: :class:`CachedDataProvider` used
to live at ``engine.providers.cached`` and was later promoted to its
canonical home at :mod:`engine.data.providers.cached` alongside the rest of
the data-provider stack. The class has a single, canonical implementation;
this shim simply re-exports it so legacy import paths keep resolving:

.. code-block:: python

    from engine.providers.cached import CachedDataProvider  # still works
    from engine.data.providers.cached import CachedDataProvider  # canonical

No behaviour is defined here — every name is imported verbatim from the
canonical module so the two paths are guaranteed to stay in sync.
"""

from __future__ import annotations

from engine.data.providers.cached import (
    DEFAULT_TTL_SECONDS,
    CachedDataProvider,
)

__all__ = ["DEFAULT_TTL_SECONDS", "CachedDataProvider"]
