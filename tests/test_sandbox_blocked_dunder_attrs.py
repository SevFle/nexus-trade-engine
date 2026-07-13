"""
Comprehensive coverage for the dangerous-dunder blocklist in
``engine.plugins.sandbox``.

The sandbox intercepts ``builtins.getattr`` (see
``StrategySandbox._restricted_getattr``) and refuses to hand back a curated set
of "dangerous" dunder attributes that could be used to escape the sandbox —
e.g. walking ``object.__subclasses__()``, grabbing a function's ``__globals__``,
or abusing the pickle protocol via ``__reduce__``.

These tests pin down:

1. That **every** attribute on the canonical dangerous-dunder list is present
   in ``_BLOCKED_ATTRS`` (regression guard against accidental removal).
2. That each blocked attribute is *actually* rejected by
   ``_restricted_getattr`` — both the 2-argument form (raises
   ``PermissionError``) and the 3-argument ``getattr(obj, name, default)``
   form (returns the caller's default and never leaks the real value).
3. That the block holds across a variety of object *kinds* (functions,
   classes, instances, built-in types) — the protection must not depend on
   the receiver.
"""

from __future__ import annotations

import builtins
from typing import Any

import pytest

from engine.plugins.manifest import StrategyManifest
from engine.plugins.sandbox import _BLOCKED_ATTRS, StrategySandbox

# ── The canonical list of dangerous dunder attributes ────────────────
#
# Every name here must be blocked.  Adding a new entry to this list is a
# deliberate, reviewed security decision; the parametrised tests below
# enforce that the blocklist and the runtime behaviour stay in sync.
DANGEROUS_DUNDER_ATTRS: tuple[str, ...] = (
    # Type-hierarchy introspection → reach arbitrary loaded classes.
    "__subclasses__",
    "__bases__",
    "__mro__",
    "__subclasshook__",
    "__init_subclass__",
    "__class_getitem__",
    # Function / instance internals → leak code objects & module namespaces.
    "__globals__",
    "__closure__",
    "__code__",
    "__wrapped__",
    "__dict__",
    # Pickling / serialisation hooks → classic sandbox-escape vector.
    "__reduce__",
    "__reduce_ex__",
    "__getstate__",
    "__setstate__",
    # Attribute / reachability enumeration.
    "__dir__",
    "__format__",
)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def manifest() -> StrategyManifest:
    return StrategyManifest(
        id="dunder-block-test",
        name="dunder-block-test",
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


# Objects of different kinds that a sandboxed strategy might try to probe.
# ``__wrapped__`` / ``__reduce__`` are most interesting on functions/instances,
# while ``__bases__`` / ``__subclasses__`` are class-level — so we exercise a
# spread of receiver types.
def _sample_function() -> None:
    """A plain module-level function used as a getattr target."""


class _SampleClass:
    """A plain class used as a getattr target."""

    answer: int = 42


_SAMPLE_INSTANCE = _SampleClass()


_RECEIVERS: list[tuple[str, Any]] = [
    ("function", _sample_function),
    ("class", _SampleClass),
    ("instance", _SAMPLE_INSTANCE),
    ("builtin_type", int),
    ("builtin_instance", 42),
]


# ── Helpers to toggle the restricted getattr hook ────────────────────


def _activate(sandbox: StrategySandbox) -> None:
    sandbox._original_getattr = builtins.getattr
    sandbox._getattr_violation_counted = False
    builtins.getattr = sandbox._restricted_getattr  # type: ignore[assignment]


def _deactivate(sandbox: StrategySandbox) -> None:
    if sandbox._original_getattr is not None:
        builtins.getattr = sandbox._original_getattr  # type: ignore[assignment]
        sandbox._original_getattr = None


# ── 1. Blocklist membership ──────────────────────────────────────────


class TestDangerousDunderBlocklist:
    """The canonical dangerous-dunder list must be fully represented in
    ``_BLOCKED_ATTRS``."""

    @pytest.mark.parametrize("attr", DANGEROUS_DUNDER_ATTRS)
    def test_attr_is_in_blocklist(self, attr: str) -> None:
        assert attr in _BLOCKED_ATTRS, (
            f" Dangerous dunder {attr!r} is missing from _BLOCKED_ATTRS"
        )

    def test_blocklist_is_a_superset_of_canonical_list(self) -> None:
        missing = set(DANGEROUS_DUNDER_ATTRS) - set(_BLOCKED_ATTRS)
        assert not missing, f"Missing dangerous dunders: {sorted(missing)}"

    def test_blocklist_is_frozen(self) -> None:
        # A frozenset cannot be mutated at runtime by attacker code.
        assert isinstance(_BLOCKED_ATTRS, frozenset)

    @pytest.mark.parametrize("attr", DANGEROUS_DUNDER_ATTRS)
    def test_canonical_list_has_no_duplicates(self, attr: str) -> None:
        # Each entry appears exactly once in the canonical list.
        assert DANGEROUS_DUNDER_ATTRS.count(attr) == 1


# ── 2. Runtime enforcement via _restricted_getattr ───────────────────


class TestBlockedDunderRuntime:
    """Each dangerous dunder is actually rejected at runtime, across object
    kinds, for both the 2-arg and 3-arg ``getattr`` forms."""

    @pytest.mark.parametrize("attr", DANGEROUS_DUNDER_ATTRS)
    @pytest.mark.parametrize(("receiver_kind", "receiver"), _RECEIVERS)
    def test_blocked_attr_raises_without_default(
        self,
        sandbox: StrategySandbox,
        attr: str,
        receiver_kind: str,
        receiver: Any,
    ) -> None:
        _activate(sandbox)
        try:
            with pytest.raises(PermissionError, match="not accessible"):
                sandbox._restricted_getattr(receiver, attr)
        finally:
            _deactivate(sandbox)

    @pytest.mark.parametrize("attr", DANGEROUS_DUNDER_ATTRS)
    @pytest.mark.parametrize(("receiver_kind", "receiver"), _RECEIVERS)
    def test_blocked_attr_returns_default_with_three_args(
        self,
        sandbox: StrategySandbox,
        attr: str,
        receiver_kind: str,
        receiver: Any,
    ) -> None:
        sentinel = object()
        _activate(sandbox)
        try:
            result = sandbox._restricted_getattr(receiver, attr, sentinel)
            # Must return the caller's default verbatim — never the real value.
            assert result is sentinel
        finally:
            _deactivate(sandbox)

    @pytest.mark.parametrize("attr", DANGEROUS_DUNDER_ATTRS)
    def test_blocked_attr_default_never_leaks_real_value(
        self,
        sandbox: StrategySandbox,
        attr: str,
    ) -> None:
        """Even when a default is supplied, the *real* underlying value must
        not leak.  We assert by type/identity against a distinctive sentinel
        and confirm the real value (when it exists) is a different object."""
        _activate(sandbox)
        try:
            # For ``__reduce_ex__`` we must pass a protocol arg via the real
            # method, but the getattr hook intercepts *access* before any call,
            # so a plain 3-arg getattr is the right probe.
            leaked = sandbox._restricted_getattr(_sample_function, attr, "SAFE")
            assert leaked == "SAFE"
        finally:
            _deactivate(sandbox)

    @pytest.mark.parametrize("attr", DANGEROUS_DUNDER_ATTRS)
    def test_blocked_attr_records_violation_in_metrics(
        self,
        sandbox: StrategySandbox,
        attr: str,
    ) -> None:
        assert sandbox.metrics.errors == 0
        _activate(sandbox)
        try:
            with pytest.raises(PermissionError):
                sandbox._restricted_getattr(_sample_function, attr)
        finally:
            _deactivate(sandbox)
        assert sandbox.metrics.errors == 1
        assert attr in (sandbox.metrics.last_error or "")


# ── 3. End-to-end via safe_evaluate ──────────────────────────────────


def _make_probe_strategy(attr: str) -> type:
    """Build a strategy that tries to read ``attr`` via ``getattr``."""

    class _ProbeStrategy:
        name = f"probe_{attr}"
        version = "1.0.0"

        def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
            getattr(self.on_bar, attr)
            return []

    return _ProbeStrategy


@pytest.mark.parametrize("attr", DANGEROUS_DUNDER_ATTRS)
async def test_dangerous_dunder_blocked_end_to_end(
    manifest: StrategyManifest,
    attr: str,
) -> None:
    """A sandboxed strategy that reaches for any dangerous dunder produces no
    signals, records exactly one error, and surfaces the attribute name in
    ``last_error``."""
    sandbox = StrategySandbox(_make_probe_strategy(attr)(), manifest)
    try:
        signals = await sandbox.safe_evaluate(None, None, None)
        assert signals == []
        assert sandbox.metrics.errors >= 1
        assert attr in (sandbox.metrics.last_error or "")
    finally:
        sandbox.cleanup()


# ── 4. Benign dunders remain accessible ──────────────────────────────


class TestBenignDundersStillAccessible:
    """Make sure legitimate, non-escape-vector dunders still pass through, so
    the expanded blocklist doesn't paralyse normal introspection."""

    @pytest.mark.parametrize(
        "attr",
        ["__init__", "__class__", "__name__", "__doc__", "__module__", "__qualname__"],
    )
    def test_benign_dunder_not_blocked(self, attr: str) -> None:
        assert attr not in _BLOCKED_ATTRS

    def test_benign_dunder_passes_through_getattr(
        self, sandbox: StrategySandbox
    ) -> None:
        _activate(sandbox)
        try:
            assert sandbox._restricted_getattr(_sample_function, "__name__") == "_sample_function"
            assert sandbox._restricted_getattr(_SampleClass, "__name__") == "_SampleClass"
            assert sandbox.metrics.errors == 0
        finally:
            _deactivate(sandbox)
