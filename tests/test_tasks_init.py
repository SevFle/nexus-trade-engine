"""Tests for the deprecated facade in :mod:`engine.tasks` (``__init__.py``).

The package exposes a PEP 562 module-level ``__getattr__`` that lazily
re-exports ``broker``, ``run_backtest_task`` and ``scheduler`` from
:mod:`engine.tasks.worker` while emitting a ``DeprecationWarning``. The
``__dir__`` advertises exactly that facade surface. Submodule imports
(e.g. ``from engine.tasks.broker import broker``) must resolve through the
normal import machinery and stay warning-free.

These tests pin that contract so the deprecation surface — plus the
``__dir__`` / ``AttributeError`` behaviour — stays covered.
"""

from __future__ import annotations

import importlib
import sys
import warnings

import pytest

# Names the historical facade re-exported from ``engine.tasks.worker``.
_FACADE_NAMES = ("broker", "run_backtest_task", "scheduler")


# NOTE: This suite is NOT parallel-safe with any other test that imports an
# ``engine.tasks`` submodule in the same process. Importing
# ``engine.tasks.broker`` binds ``broker`` as an attribute on the shared
# package object, which would shadow the deprecating ``__getattr__`` for that
# name. The isolation below relies on a known per-process import ordering, so
# these tests must run together (and not concurrently with such importers).
@pytest.fixture(scope="module")
def tasks_package():
    """Yield a freshly-imported ``engine.tasks`` package.

    All ``engine.tasks*`` keys are removed from :data:`sys.modules` and the
    package is re-imported so the facade ``__getattr__`` is exercised from a
    clean state (e.g. the ``broker`` submodule is not yet bound as a package
    attribute, which would otherwise shadow the deprecating lookup).

    Scope/caveats:

    * This isolates **only the** ``sys.modules`` **keys** for the
      ``engine.tasks`` subtree. It does **not** invalidate live object
      references that other code elsewhere in the process may still hold to
      the previous package object, so this is a per-process guarantee rather
      than a true sandbox.
    * Importing an ``engine.tasks`` submodule mutates the shared package
      object, so the suite is not parallel-safe with such importers (see the
      comment above).

    The original :data:`sys.modules` entries are restored on teardown so the
    tests do not leak half-imported modules into the rest of the suite.

    The fixture is module-scoped: a single fresh import is shared across the
    facade tests because none of them binds a facade name as a package
    attribute (so the deprecating ``__getattr__`` keeps firing on every
    access), and ``dir(engine.tasks)`` reflects only the module-level
    ``__dir__`` regardless of other bound attributes.
    """
    mods_to_remove = [k for k in sys.modules if k.startswith("engine.tasks")]
    saved = {m: sys.modules.pop(m) for m in mods_to_remove}
    try:
        tasks = importlib.import_module("engine.tasks")
        yield tasks
    finally:
        for m in [k for k in sys.modules if k.startswith("engine.tasks")]:
            sys.modules.pop(m, None)
        sys.modules.update(saved)


def _access_with_warning(tasks, name):
    """Return ``(value, deprecation_warnings)`` for a facade attribute read."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        value = getattr(tasks, name)
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    return value, deprecations


@pytest.mark.parametrize("name", _FACADE_NAMES)
def test_facade_attribute_emits_deprecation(tasks_package, name):
    """Each facade attribute fires exactly one ``DeprecationWarning``.

    The value returned must be the real object living on
    :mod:`engine.tasks.worker`, so existing callers keep working while being
    nudged toward the canonical import.
    """
    value, deprecations = _access_with_warning(tasks_package, name)
    assert deprecations, f"expected a DeprecationWarning for the {name!r} facade"
    assert len(deprecations) == 1
    message = str(deprecations[0].message)
    assert "engine.tasks" in message
    assert "deprecated" in message.lower()
    # The warning should point at the call site (stacklevel=2), not the
    # facade internals — this keeps the deprecation actionable.
    assert deprecations[0].filename == __file__

    from engine.tasks import worker

    assert value is getattr(worker, name)


def test_facade_warning_message_names_replacement_module(tasks_package):
    """The warning text steers users toward :mod:`engine.tasks.worker`."""
    _, deprecations = _access_with_warning(tasks_package, "scheduler")
    assert "engine.tasks.worker" in str(deprecations[0].message)


def test_dir_advertises_exactly_the_facade_names(tasks_package):
    """``dir(engine.tasks)`` returns the (sorted) facade surface."""
    advertised = dir(tasks_package)
    assert advertised == sorted(_FACADE_NAMES)


def test_unknown_attribute_raises_attribute_error(tasks_package):
    """Names outside the facade surface raise ``AttributeError``."""
    with pytest.raises(AttributeError, match="this_is_not_a_facade_name"):
        # Assigned (rather than a bare expression) so the attribute access is
        # not flagged as a useless statement (ruff B018); it still executes
        # inside ``pytest.raises`` and is expected to raise.
        _ = tasks_package.this_is_not_a_facade_name


def test_submodule_import_is_warning_free():
    """Importing a submodule directly must not trip the facade warning.

    ``from engine.tasks.broker import broker`` resolves through the normal
    import machinery, never reaching ``__getattr__``; the app factory relies
    on this staying silent.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        module = importlib.import_module("engine.tasks.broker")
        # Touch the attribute to be sure no lazy ``__getattr__`` fires.
        assert module.broker is not None
    assert not [
        w for w in caught if issubclass(w.category, DeprecationWarning)
    ]
