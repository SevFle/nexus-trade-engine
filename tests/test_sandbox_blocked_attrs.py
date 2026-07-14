"""
Integration tests for the expanded ``_BLOCKED_ATTRS`` set.

These tests exercise the full ``StrategySandbox.safe_evaluate`` pipeline against
the canonical CPython sandbox-escape chains, verifying that *every* step on
each chain is denied by :data:`_BLOCKED_ATTRS` and recorded in
``sandbox.metrics.errors``.

The three escape chains pinned down here were the ones that produced a silent
``errors == 0`` (escape *succeeded*) before the block list was widened to cover
the entry-point dunders used by each chain:

  * **class chain** — ``().__class__`` is the entry point to
    ``__bases__`` / ``__subclasses__`` traversal.  Before ``__class__`` was
    promoted into ``_BLOCKED_ATTRS`` the very first ``getattr`` returned the
    real ``tuple`` type, letting the rest of the chain proceed.
  * **function unwrap** — a bound method's ``__func__`` re-exposes the
    underlying function and therefore its ``__globals__`` / ``__code__`` /
    ``__closure__``.  ``__func__`` was absent from the original list.
  * **base traversal** — ``__base__`` is the single-inheritance fast-path that
    ``__bases__`` generalises; it was missing from the list as well.

The strategies deliberately use the *dynamic* ``getattr(obj, name)`` form: that
is the only attribute-access builtin the sandbox can hook from pure Python
(see the ``_BLOCKED_ATTRS`` docstring in ``engine/plugins/sandbox/__init__.py``).
Direct dotted access (``obj.__class__``) is governed separately by Layer-5
process isolation, which is out of scope for this in-process MVP.
"""

from __future__ import annotations

from typing import Any

import pytest

from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox import StrategySandbox

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def manifest() -> StrategyManifest:
    return StrategyManifest(
        id="escape-chain-test",
        name="escape-chain-test",
        version="1.0.0",
        resources={"max_cpu_seconds": 1},
    )


# ── Adversarial strategies (one per escape chain) ────────────────────


class _ClassChainStrategy:
    """Classic type-hierarchy traversal.

    ``().__class__`` -> ``__bases__[0]`` (``object``) -> ``__subclasses__()``.

    In a real attack the subclass list is scanned for gadgets such as
    ``subprocess.Popen`` or ``os._wrap_close``.  Here we only need to reach
    ``__subclasses__`` to prove the chain is broken — the very first step
    (``getattr((), '__class__')``) is now denied.
    """

    name = "class_chain"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        sentinel = ()
        cls = getattr(sentinel, "__class__")  # noqa: B009
        bases = cls.__bases__
        root = bases[0]
        subs = getattr(root, "__subclasses__")()  # noqa: B009
        return [getattr(t, "__name__") for t in subs]  # noqa: B009


class _FuncUnwrapStrategy:
    """Bound-method unwrap chain.

    ``self.on_bar.__func__`` re-exposes the underlying function object, from
    which ``__globals__`` (the module namespace, including the real
    ``builtins``), ``__code__`` and ``__closure__`` all hang off.  Blocking
    ``__func__`` cuts the unwrap before any of those are reachable.
    """

    name = "func_unwrap"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        bound = self.on_bar
        fn = getattr(bound, "__func__")  # noqa: B009
        globs = getattr(fn, "__globals__")  # noqa: B009
        # If we ever get here the sandbox failed: enumerate the dangerous
        # module references reachable from the function's globals.
        return [k for k in globs if "os" in k]


class _BaseTraversalStrategy:
    """Single-inheritance ``__base__`` walk.

    ``type.__base__`` climbs one level up the MRO; chained with
    ``__subclasses__`` it reaches arbitrary leaf types just like
    ``__bases__``.  ``__base__`` was the missing sibling of ``__bases__``.
    """

    name = "base_traversal"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        sentinel = ()
        cls = getattr(sentinel, "__class__")  # noqa: B009
        parent = getattr(cls, "__base__")  # noqa: B009 -- ``object``
        subs = getattr(parent, "__subclasses__")()  # noqa: B009
        return [getattr(t, "__name__") for t in subs]  # noqa: B009


# ── Integration tests ─────────────────────────────────────────────────


class TestEscapeChainIntegration:
    """End-to-end: each canonical escape chain must be broken and counted."""

    @pytest.mark.asyncio
    async def test_class_chain_blocked(self, manifest: StrategyManifest) -> None:
        """The ``__class__`` -> ``__bases__`` -> ``__subclasses__`` chain is
        broken at the first ``getattr`` and the violation is recorded."""
        sandbox = StrategySandbox(_ClassChainStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            # The escape attempt must surface as a security violation.
            assert sandbox.metrics.errors >= 1
            assert "not accessible" in (sandbox.metrics.last_error or "")
            # The very first probe (``__class__``) is the one denied.
            assert "__class__" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()

    @pytest.mark.asyncio
    async def test_func_unwrap_blocked(self, manifest: StrategyManifest) -> None:
        """The bound-method ``__func__`` unwrap is denied before ``__globals__``
        can be reached."""
        sandbox = StrategySandbox(_FuncUnwrapStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
            assert "not accessible" in (sandbox.metrics.last_error or "")
            # ``__func__`` is the entry point of this chain.
            assert "__func__" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()

    @pytest.mark.asyncio
    async def test_base_traversal_blocked(self, manifest: StrategyManifest) -> None:
        """The ``__class__`` -> ``__base__`` -> ``__subclasses__`` walk is
        denied at the first step."""
        sandbox = StrategySandbox(_BaseTraversalStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors >= 1
            assert "not accessible" in (sandbox.metrics.last_error or "")
            # The chain begins with ``__class__`` (which is now blocked), so
            # ``__base__`` is never reached.
            assert "__class__" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()

    # ── Regression: legitimate code is unaffected ──────────────────────

    @pytest.mark.asyncio
    async def test_isinstance_still_works(self, manifest: StrategyManifest) -> None:
        """``isinstance`` / ``type`` checks use the C-level type machinery, not
        the ``getattr`` hook, so blocking ``__class__`` must not break them.

        This guards against the regression flagged when ``__class__`` was
        promoted into ``_BLOCKED_ATTRS``: real strategies routinely use
        ``isinstance`` for dispatch, and those calls must keep working.
        """

        class _IsinstanceStrategy:
            name = "isinstance_ok"
            version = "1.0.0"

            def on_bar(self, state: Any, _portfolio: Any) -> list[Any]:
                if isinstance(state, dict):
                    return []
                if isinstance(self, _IsinstanceStrategy):
                    return []
                # ``type(...)`` is the other legitimate ``__class__`` consumer.
                _ = type(state)
                return []

        sandbox = StrategySandbox(_IsinstanceStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 0
            assert sandbox.metrics.last_error is None
        finally:
            sandbox.cleanup()
