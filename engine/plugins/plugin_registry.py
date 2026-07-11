"""In-memory registry of ``IStrategy`` plugin classes.

This registry is the runtime counterpart to the file-system discovery
registry in :mod:`engine.plugins.registry`.  Discovery scans
``strategies/`` for *installed* strategy manifests on disk; this module
tracks *loaded* strategy **classes** that are ready to be instantiated and
handed to orchestrators, the MCP server, or the strategy API.

The registry is intentionally minimal and synchronous:

* :meth:`PluginRegistry.register` validates a class against the
  :class:`nexus_sdk.strategy.IStrategy` interface at registration time so a
  malformed plugin can never reach the runtime.
* Names are unique — re-registering a name raises
  :class:`DuplicatePluginError`.
* Removing or looking up an unknown name raises
  :class:`PluginNotFoundError` (for removal) or returns ``None`` (for
  :meth:`get`), matching dict-mapping conventions while still surfacing
  programmer mistakes loudly.
* :meth:`list_all` returns a defensive **copy** so callers cannot mutate
  internal state.

All errors derive from :class:`PluginError`, giving callers a single
``except PluginError`` to trap every registry failure.
"""

from __future__ import annotations

import inspect
from typing import Any

import structlog

logger = structlog.get_logger()

try:
    from nexus_sdk.strategy import IStrategy as _IStrategy
except ImportError:  # pragma: no cover - SDK present in CI/test envs
    logger.warning("istrategy_sdk_unavailable")
    _IStrategy = None


def _load_istrategy() -> type | None:
    """Return the ``IStrategy`` ABC, or ``None`` when the SDK is unavailable.

    The SDK lives outside the engine package and is optional for some
    deployments, so a missing ``nexus_sdk`` degrades gracefully to
    "interface validation disabled" instead of crashing import.
    """
    return _IStrategy


class PluginError(Exception):
    """Base class for every plugin-registry failure.

    Catch this to handle *any* registry error in one ``except`` block;
    more specific subclasses (:class:`DuplicatePluginError`,
    :class:`PluginNotFoundError`) let callers branch on the failure mode.
    """


class DuplicatePluginError(PluginError):
    """Raised by :meth:`PluginRegistry.register` for a name already in use."""


class PluginNotFoundError(PluginError):
    """Raised when a name is not registered.

    Used by :meth:`PluginRegistry.unregister`; :meth:`get` returns ``None``
    instead so it composes with dict-style optional lookups.
    """


class PluginRegistry:
    """In-memory, name-keyed registry of ``IStrategy`` strategy classes.

    The registry stores **classes** (not instances): instantiation is the
    caller's responsibility and may be expensive or async.  All mutation
    happens through the explicit methods below; the underlying mapping is
    never exposed.
    """

    def __init__(self) -> None:
        self._plugins: dict[str, type] = {}

    # ------------------------------------------------------------------ #
    # Mutation
    # ------------------------------------------------------------------ #
    def register(self, name: str, plugin: Any) -> None:
        """Register ``plugin`` (a class) under ``name``.

        Validates at registration time:

        1. ``name`` is a ``str`` and not empty.
        2. ``plugin`` is a class (rejects instances and callables).
        3. ``plugin`` is a subclass of :class:`nexus_sdk.strategy.IStrategy`
           (rejects bare classes and unrelated classes).
        4. ``plugin`` has no remaining abstract methods (rejects partial
           subclasses of ``IStrategy`` that forgot to implement ``evaluate``,
           ``initialize``, etc.).
        5. ``name`` is not already registered.

        Raises:
            PluginError: if any validation (1)-(4) fails.
            DuplicatePluginError: if ``name`` is already registered.
        """
        if not isinstance(name, str) or not name:
            raise PluginError(
                f"Plugin name must be a non-empty str, got {name!r} "
                f"(type {type(name).__name__})"
            )
        if not inspect.isclass(plugin):
            raise PluginError(
                f"Plugin '{name}' must be a class, got "
                f"{type(plugin).__name__}: {plugin!r}"
            )
        istrategy = _load_istrategy()
        if istrategy is not None and not issubclass(plugin, istrategy):
            raise PluginError(
                f"Plugin '{name}' (class {plugin.__name__}) must be a subclass "
                f"of nexus_sdk.strategy.IStrategy"
            )
        remaining = getattr(plugin, "__abstractmethods__", None)
        if remaining:
            raise PluginError(
                f"Plugin '{name}' (class {plugin.__name__}) has unimplemented "
                f"abstract methods: {sorted(remaining)}"
            )
        if name in self._plugins:
            raise DuplicatePluginError(
                f"Plugin '{name}' is already registered "
                f"(existing class {self._plugins[name].__name__})"
            )
        logger.info("plugin_registered", name=name, plugin=plugin.__name__)
        self._plugins[name] = plugin

    def unregister(self, name: str) -> None:
        """Remove ``name`` from the registry.

        Raises:
            PluginNotFoundError: if ``name`` is not registered.
        """
        if name not in self._plugins:
            raise PluginNotFoundError(f"Plugin '{name}' is not registered")
        removed = self._plugins.pop(name)
        logger.info("plugin_unregistered", name=name, plugin=removed.__name__)

    def clear(self) -> None:
        """Remove every registered plugin."""
        count = len(self._plugins)
        self._plugins.clear()
        if count:
            logger.info("plugins_cleared", count=count)

    # ------------------------------------------------------------------ #
    # Read access
    # ------------------------------------------------------------------ #
    def get(self, name: str) -> type | None:
        """Return the class registered under ``name`` or ``None`` if absent."""
        return self._plugins.get(name)

    def list_all(self) -> list[str]:
        """Return a **copy** of all registered plugin names.

        Returning a fresh list (rather than the live ``dict_keys`` view) keeps
        callers from mutating internal state by inserting or deleting into the
        returned object, and decouples iteration order from later mutation.
        """
        return list(self._plugins.keys())

    # ------------------------------------------------------------------ #
    # Dunder protocol
    # ------------------------------------------------------------------ #
    def __contains__(self, name: object) -> bool:
        """``True`` if ``name`` is registered.

        Unhashable lookups (e.g. ``[...] in registry``) return ``False``
        rather than raising ``TypeError`` so callers can do defensive
        membership checks on untrusted input without wrapping every test in
        a try/except.
        """
        try:
            return name in self._plugins
        except TypeError:
            return False

    def __len__(self) -> int:
        """Number of registered plugins."""
        return len(self._plugins)
