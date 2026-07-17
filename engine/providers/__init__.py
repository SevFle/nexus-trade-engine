"""TTL-caching data-provider package.

Re-exports :class:`CachedDataProvider`, an in-memory TTL decorator over any
:class:`~engine.data.providers.base.IDataProvider`.
"""

from __future__ import annotations

from engine.providers.cached import DEFAULT_TTL_SECONDS, CachedDataProvider

__all__ = ["DEFAULT_TTL_SECONDS", "CachedDataProvider"]
