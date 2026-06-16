"""
Comprehensive tests for ``StrategySandbox._restricted_getattr``.

These tests pin down the behaviour introduced by commit bd0b17a ("block
__globals__ introspection via restricted getattr") and the follow-up fix
that ensures blocked-attribute *attempts* are always recorded in
``sandbox.metrics.errors`` — even when the attacker swallows the
``PermissionError`` or supplies a default argument.

Coverage areas
--------------
1. Direct ``_restricted_getattr`` unit tests (all blocked dunders).
2. Error registration / metrics accounting (the "silent swallow" bug).
3. Builtins injection — verifying the hook is actually installed and removed.
4. Default-argument contract (3-arg ``getattr``) — value never leaked.
5. Pass-through semantics for non-blocked attributes.
6. Integration via ``safe_evaluate`` for end-to-end confidence.
7. Double-counting / idempotency edge cases.
"""

from __future__ import annotations

import builtins
from typing import Any

import pytest

from engine.core.signal import Signal
from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox import _BLOCKED_ATTRS, StrategySandbox

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def manifest() -> StrategyManifest:
    return StrategyManifest(
        id="getattr-test",
        name="getattr-test",
        version="1.0.0",
        resources={"max_cpu_seconds": 1},
    )


@pytest.fixture
def sandbox(manifest: StrategyManifest) -> StrategySandbox:
    class _NoopStrategy:
        name = "noop"
        version = "1.0.0"

        def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
            return []

    sb = StrategySandbox(_NoopStrategy(), manifest)
    yield sb
    sb.cleanup()


# ── Adversarial strategy helpers ─────────────────────────────────────


class _GetattrGlobalsStrategy:
    """Direct attack: ``getattr(fn, '__globals__')`` with no default."""

    name = "getattr_globals"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        fn = self.on_bar
        globs = getattr(fn, "__globals__")  # noqa: B009
        return [k for k in globs if "os" in k]


class _SneakyCatchGlobalsStrategy:
    """Attacker swallows the PermissionError to hide the violation."""

    name = "sneaky_catch_globals"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        fn = self.on_bar
        try:
            globs = getattr(fn, "__globals__")  # noqa: B009
            return [k for k in globs if "os" in k]
        except PermissionError:
            return []


class _SneakyDefaultGlobalsStrategy:
    """Attacker uses the 3-arg getattr form to avoid the exception."""

    name = "sneaky_default_globals"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        fn = self.on_bar
        globs = getattr(fn, "__globals__", None)
        if globs is not None:
            return [k for k in globs if "os" in k]
        return []


class _MultipleBlockedAttrStrategy:
    """Attacker probes several blocked dunders; only the first raises."""

    name = "multi_blocked"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        fn = self.on_bar
        for attr in ("__globals__", "__code__", "__closure__"):
            try:
                getattr(fn, attr)
            except PermissionError:
                continue
        return []


class _GetattrSubclassesStrategy:
    """``getattr(int, '__subclasses__')`` traversal attack."""

    name = "getattr_subclasses"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        subs = getattr(int, "__subclasses__")()  # noqa: B009
        return [type(s).__name__ for s in subs]


class _NormalGetattrStrategy:
    """Legitimate ``getattr`` usage that must NOT be blocked."""

    name = "normal_getattr"
    version = "1.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        obj = type("Obj", (), {"x": 42})()
        val = obj.x
        missing = getattr(obj, "missing", "fallback")
        return [Signal.buy(symbol=f"TST{val}{missing}", strategy_id=self.name)]


# ── 1. Direct unit tests for _restricted_getattr ──────────────────────


class TestRestrictedGetattrUnit:
    """Drive ``_restricted_getattr`` directly with an activated sandbox."""

    def _activate(self, sandbox: StrategySandbox) -> None:
        sandbox._original_getattr = builtins.getattr
        sandbox._getattr_violation_counted = False
        builtins.getattr = sandbox._restricted_getattr  # type: ignore[assignment]

    def _deactivate(self, sandbox: StrategySandbox) -> None:
        if sandbox._original_getattr is not None:
            builtins.getattr = sandbox._original_getattr  # type: ignore[assignment]
            sandbox._original_getattr = None

    @pytest.mark.parametrize("attr", sorted(_BLOCKED_ATTRS))
    def test_blocked_attr_raises_without_default(
        self, sandbox: StrategySandbox, attr: str
    ) -> None:
        self._activate(sandbox)
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                sandbox._restricted_getattr(self, attr)
        finally:
            self._deactivate(sandbox)

    @pytest.mark.parametrize("attr", sorted(_BLOCKED_ATTRS))
    def test_blocked_attr_returns_default_with_three_args(
        self, sandbox: StrategySandbox, attr: str
    ) -> None:
        self._activate(sandbox)
        try:
            result = sandbox._restricted_getattr(self, attr, "sentinel")
            assert result == "sentinel"
        finally:
            self._deactivate(sandbox)

    @pytest.mark.parametrize("attr", sorted(_BLOCKED_ATTRS))
    def test_blocked_attr_increments_errors(
        self, sandbox: StrategySandbox, attr: str
    ) -> None:
        assert sandbox.metrics.errors == 0
        self._activate(sandbox)
        try:
            with pytest.raises(PermissionError):
                sandbox._restricted_getattr(self, attr)
        finally:
            self._deactivate(sandbox)
        assert sandbox.metrics.errors == 1
        assert attr in (sandbox.metrics.last_error or "")

    @pytest.mark.parametrize("attr", sorted(_BLOCKED_ATTRS))
    def test_blocked_attr_with_default_increments_errors(
        self, sandbox: StrategySandbox, attr: str
    ) -> None:
        assert sandbox.metrics.errors == 0
        self._activate(sandbox)
        try:
            result = sandbox._restricted_getattr(self, attr, None)
            assert result is None
        finally:
            self._deactivate(sandbox)
        assert sandbox.metrics.errors == 1
        assert attr in (sandbox.metrics.last_error or "")

    def test_normal_attr_passes_through(
        self, sandbox: StrategySandbox
    ) -> None:
        self._activate(sandbox)
        try:
            obj = type("T", (), {"value": 99})()
            assert sandbox._restricted_getattr(obj, "value") == 99
            assert sandbox.metrics.errors == 0
        finally:
            self._deactivate(sandbox)

    def test_missing_attr_without_default_raises_attribute_error(
        self, sandbox: StrategySandbox
    ) -> None:
        self._activate(sandbox)
        try:
            obj = type("T", (), {})()
            with pytest.raises(AttributeError):
                sandbox._restricted_getattr(obj, "nope")
            assert sandbox.metrics.errors == 0
        finally:
            self._deactivate(sandbox)

    def test_missing_attr_with_default_returns_default(
        self, sandbox: StrategySandbox
    ) -> None:
        self._activate(sandbox)
        try:
            obj = type("T", (), {})()
            assert sandbox._restricted_getattr(obj, "nope", 123) == 123
            assert sandbox.metrics.errors == 0
        finally:
            self._deactivate(sandbox)


# ── 2. Error registration via safe_evaluate (the "silent swallow" bug) ─


class TestErrorRegistrationViaEvaluate:
    async def test_direct_attack_registers_error(
        self, manifest: StrategyManifest
    ) -> None:
        """``getattr(fn, '__globals__')`` with no default → error counted."""
        sandbox = StrategySandbox(_GetattrGlobalsStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "__globals__" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()

    async def test_sneaky_catch_registers_error(
        self, manifest: StrategyManifest
    ) -> None:
        """When the attacker swallows the PermissionError, the violation
        must still be recorded in ``metrics.errors``."""
        sandbox = StrategySandbox(_SneakyCatchGlobalsStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "__globals__" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()

    async def test_sneaky_default_registers_error(
        self, manifest: StrategyManifest
    ) -> None:
        """The 3-arg ``getattr(fn, '__globals__', None)`` form must still
        be recorded as a security violation even though no exception
        propagates."""
        sandbox = StrategySandbox(_SneakyDefaultGlobalsStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "__globals__" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()

    async def test_multiple_blocked_attrs_all_counted(
        self, manifest: StrategyManifest
    ) -> None:
        """Each blocked-attribute probe is counted individually."""
        sandbox = StrategySandbox(_MultipleBlockedAttrStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 3
        finally:
            sandbox.cleanup()

    async def test_subclasses_attack_registers_error(
        self, manifest: StrategyManifest
    ) -> None:
        sandbox = StrategySandbox(_GetattrSubclassesStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert signals == []
            assert sandbox.metrics.errors == 1
            assert "__subclasses__" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()

    async def test_normal_getattr_does_not_register_error(
        self, manifest: StrategyManifest
    ) -> None:
        """Legitimate ``getattr`` calls must not inflate the error count."""
        sandbox = StrategySandbox(_NormalGetattrStrategy(), manifest)
        try:
            signals = await sandbox.safe_evaluate(None, None, None)
            assert len(signals) == 1
            assert signals[0].symbol == "TST42fallback"
            assert sandbox.metrics.errors == 0
        finally:
            sandbox.cleanup()


# ── 3. Double-counting / idempotency ─────────────────────────────────


class TestNoDoubleCounting:
    async def test_direct_attack_not_double_counted(
        self, manifest: StrategyManifest
    ) -> None:
        """When ``_restricted_getattr`` raises and the exception propagates
        to ``_evaluate_inner``, the error must be counted exactly once."""
        sandbox = StrategySandbox(_GetattrGlobalsStrategy(), manifest)
        try:
            await sandbox.safe_evaluate(None, None, None)
            assert sandbox.metrics.errors == 1
        finally:
            sandbox.cleanup()

    async def test_flag_resets_between_evaluations(
        self, manifest: StrategyManifest
    ) -> None:
        """The ``_getattr_violation_counted`` flag must reset between
        evaluations so a *later* non-getattr error is still counted."""
        sandbox = StrategySandbox(_SneakyCatchGlobalsStrategy(), manifest)
        try:
            # First evaluation: getattr violation (counted by _restricted_getattr)
            await sandbox.safe_evaluate(None, None, None)
            assert sandbox.metrics.errors == 1
            assert sandbox._getattr_violation_counted is True

            # Swap to a crashing strategy and re-evaluate
            class _CrashStrategy:
                name = "crash"
                version = "1.0.0"

                def on_bar(self, _s, _p):
                    raise RuntimeError("boom")

            sandbox.strategy = _CrashStrategy()
            await sandbox.safe_evaluate(None, None, None)
            # RuntimeError is a different error → counted by _evaluate_inner
            assert sandbox.metrics.errors == 2
            assert "boom" in (sandbox.metrics.last_error or "")
        finally:
            sandbox.cleanup()

    async def test_good_strategy_keeps_zero_errors_across_evals(
        self, manifest: StrategyManifest
    ) -> None:
        sandbox = StrategySandbox(_NormalGetattrStrategy(), manifest)
        try:
            for _ in range(3):
                await sandbox.safe_evaluate(None, None, None)
            assert sandbox.metrics.errors == 0
        finally:
            sandbox.cleanup()


# ── 4. Builtins injection ────────────────────────────────────────────


class TestBuiltinsInjection:
    def test_getattr_replaced_on_activate(self, sandbox: StrategySandbox) -> None:
        original = builtins.getattr
        sandbox._activate_restrictions()
        try:
            # ``_restricted_getattr`` is a bound method; compare the wrapped
            # function rather than bound-method identity.
            assert builtins.getattr.__func__ is StrategySandbox._restricted_getattr
            assert builtins.getattr is not original
        finally:
            sandbox._deactivate_restrictions()

    def test_getattr_restored_on_deactivate(self, sandbox: StrategySandbox) -> None:
        original = builtins.getattr
        sandbox._activate_restrictions()
        sandbox._deactivate_restrictions()
        assert builtins.getattr is original
        assert sandbox._original_getattr is None

    def test_getattr_restored_on_cleanup(self, sandbox: StrategySandbox) -> None:
        original = builtins.getattr
        sandbox._activate_restrictions()
        sandbox.cleanup()
        assert builtins.getattr is original

    def test_strategy_getattr_goes_through_hook(
        self, manifest: StrategyManifest
    ) -> None:
        """Inside an activated sandbox, a bare ``getattr()`` call is
        intercepted — proving the hook is installed in the namespace the
        strategy code actually uses."""
        sandbox = StrategySandbox(_GetattrGlobalsStrategy(), manifest)
        sandbox._activate_restrictions()
        try:
            assert builtins.getattr.__func__ is StrategySandbox._restricted_getattr
            with pytest.raises(PermissionError, match="__globals__"):
                getattr(sandbox.strategy.on_bar, "__globals__")  # noqa: B009
            assert sandbox.metrics.errors == 1
        finally:
            sandbox._deactivate_restrictions()


# ── 5. Default-argument value safety ─────────────────────────────────


class TestDefaultArgumentSafety:
    def test_default_never_leaks_real_globals(self, sandbox: StrategySandbox) -> None:
        """The 3-arg form must return the caller's default, never the real
        ``__globals__`` dict."""
        def _sample_fn() -> None:
            pass

        sandbox._activate_restrictions()
        try:
            result = getattr(_sample_fn, "__globals__", "SAFE")
            assert result == "SAFE"
        finally:
            sandbox._deactivate_restrictions()

    @pytest.mark.parametrize("default_val", [None, 0, "", [], {}, False, 42, "marker"])
    def test_various_defaults_returned_verbatim(
        self, sandbox: StrategySandbox, default_val: Any
    ) -> None:
        sandbox._activate_restrictions()
        try:
            result = sandbox._restricted_getattr(self, "__globals__", default_val)
            assert result is default_val or result == default_val
        finally:
            sandbox._deactivate_restrictions()


# ── 6. Blocked-attribute set completeness ────────────────────────────


class TestBlockedAttributeSet:
    def test_globals_is_blocked(self) -> None:
        assert "__globals__" in _BLOCKED_ATTRS

    def test_subclasses_is_blocked(self) -> None:
        assert "__subclasses__" in _BLOCKED_ATTRS

    def test_bases_is_blocked(self) -> None:
        assert "__bases__" in _BLOCKED_ATTRS

    def test_mro_is_blocked(self) -> None:
        assert "__mro__" in _BLOCKED_ATTRS

    def test_closure_is_blocked(self) -> None:
        assert "__closure__" in _BLOCKED_ATTRS

    def test_code_is_blocked(self) -> None:
        assert "__code__" in _BLOCKED_ATTRS

    def test_blocked_set_is_frozen(self) -> None:
        assert isinstance(_BLOCKED_ATTRS, frozenset)

    def test_normal_dunder_not_blocked(self) -> None:
        assert "__init__" not in _BLOCKED_ATTRS
        assert "__class__" not in _BLOCKED_ATTRS
        assert "__dict__" not in _BLOCKED_ATTRS
        assert "__name__" not in _BLOCKED_ATTRS
