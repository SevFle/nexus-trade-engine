"""Targeted unit tests for ``engine.tasks.__init__`` (PEP 562 facade).

These tests lift coverage of ``engine/tasks/__init__.py`` from ~62% to
near 100% by exercising the previously-uncovered branches:

* lines 43-52: the deprecation warning emission and lazy
  ``getattr(worker, name)`` resolution inside ``__getattr__``.
* line 62: the ``__dir__`` helper.

The facade deliberately defers the ``engine.tasks.worker`` import until
first deprecated-attribute access (so submodule imports stay silent),
and ``worker`` itself imports ``engine.tasks.broker`` which talks to
Redis/Valkey. Tests therefore mock the broker-construction primitives
the same way ``tests/test_tasks_module.py`` does, and isolate
``sys.modules`` so the deferred import runs through the patched path.
"""

from __future__ import annotations

import sys
import warnings
from unittest.mock import MagicMock, patch

import pytest

# Sentinel objects so we can assert the facade returns *exactly* what
# ``worker`` exposes, without depending on a live broker/scheduler.
_FAKE_BROKER = object()
_FAKE_SCHEDULER = object()
_FAKE_RUN_BACKTEST = object()


@pytest.fixture(autouse=True)
def _isolate_tasks_package(monkeypatch):
    """Reset ``engine.tasks`` import state around every test.

    PEP 562 ``__getattr__`` caches nothing itself, but ``engine.tasks.worker``
    ends up cached in ``sys.modules`` after first access. We start each test
    from a clean slate so the deferred import path always runs through the
    patched primitives.
    """
    # Snapshot and clear the entire ``engine.tasks*`` namespace.
    removed = [k for k in sys.modules if k.startswith("engine.tasks")]
    saved = {m: sys.modules.pop(m) for m in removed}

    # Build a mocked broker instance that satisfies the chained builder API
    # used in ``engine.tasks.broker.build_broker``.
    mock_broker_inst = MagicMock(name="broker_instance")
    mock_broker_inst.with_result_backend.return_value = mock_broker_inst
    mock_broker_inst.with_middlewares.return_value = mock_broker_inst

    # Make a fake ``engine.tasks.worker`` module so the facade's
    # ``from engine.tasks import worker`` lands on something controlled.
    fake_worker = MagicMock(name="engine.tasks.worker")
    fake_worker.broker = _FAKE_BROKER
    fake_worker.scheduler = _FAKE_SCHEDULER
    fake_worker.run_backtest_task = _FAKE_RUN_BACKTEST

    with (
        patch("engine.tasks.broker.ListQueueBroker", return_value=mock_broker_inst),
        patch("engine.tasks.broker.RedisAsyncResultBackend"),
        patch("engine.tasks.broker.CorrelationMiddleware"),
        patch("engine.tasks.broker.TaskiqScheduler"),
    ):
        # Pre-seed the worker module so the deferred ``from engine.tasks
        # import worker`` inside ``__getattr__`` resolves to our fake.
        monkeypatch.setitem(sys.modules, "engine.tasks.worker", fake_worker)
        # Also clear the parent package so its PEP 562 state reloads fresh.
        sys.modules.pop("engine.tasks", None)
        # Re-import the parent package (without the worker submodule).
        import importlib

        import engine.tasks

        importlib.reload(engine.tasks)
        yield engine.tasks

    # Restore sys.modules exactly as it was.
    for m in removed:
        sys.modules.pop(m, None)
    sys.modules.update(saved)


class TestDeprecatedFacadeGetattr:
    """Cover lines 43-52: deprecation warning + lazy attribute resolution."""

    def test_broker_access_emits_deprecation_warning(self, _isolate_tasks_package):
        tasks = _isolate_tasks_package
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            value = tasks.broker  # noqa: F841 — triggers __getattr__

        assert len(caught) == 1
        warning = caught[0]
        assert issubclass(warning.category, DeprecationWarning)
        assert "engine.tasks" in str(warning.message)
        assert "engine.tasks.worker" in str(warning.message)

    def test_broker_returns_worker_attribute(self, _isolate_tasks_package):
        tasks = _isolate_tasks_package
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert tasks.broker is _FAKE_BROKER

    def test_scheduler_returns_worker_attribute(self, _isolate_tasks_package):
        tasks = _isolate_tasks_package
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert tasks.scheduler is _FAKE_SCHEDULER

    def test_run_backtest_task_returns_worker_attribute(self, _isolate_tasks_package):
        tasks = _isolate_tasks_package
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            assert tasks.run_backtest_task is _FAKE_RUN_BACKTEST

    def test_deprecation_warning_uses_correct_stacklevel(self, _isolate_tasks_package):
        """The warning should point at the caller, not the facade itself."""
        tasks = _isolate_tasks_package

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _ = tasks.broker

        assert caught, "expected a DeprecationWarning"
        # stacklevel=2 means the recorded frame should be this test file.
        assert caught[0].filename == __file__

    @pytest.mark.parametrize("missing", ["broker", "scheduler", "run_backtest_task"])
    def test_defensive_attribute_error_when_worker_lacks_name(
        self, _isolate_tasks_package, missing
    ):
        """Cover the ``except AttributeError`` branch (lines 53-56).

        Although marked ``pragma: no cover`` in source, exercising it here
        documents the contract: if a future ``worker`` drops a facade name
        the error message must name the missing attribute.
        """
        tasks = _isolate_tasks_package
        # Strip the requested attribute off the fake worker so the inner
        # ``getattr(worker, name)`` raises AttributeError.
        delattr(sys.modules["engine.tasks.worker"], missing)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with pytest.raises(AttributeError) as excinfo:
                getattr(tasks, missing)

        assert f"'engine.tasks' has no attribute {missing!r}" in str(excinfo.value)


class TestNonFacadeNameRaisesAttributeError:
    """Cover line 57: the fallthrough ``raise AttributeError`` for unknown names."""

    def test_unknown_name_raises_attribute_error(self, _isolate_tasks_package):
        tasks = _isolate_tasks_package
        with pytest.raises(AttributeError) as excinfo:
            _ = tasks.totally_made_up_name

        assert "'engine.tasks' has no attribute 'totally_made_up_name'" in str(
            excinfo.value
        )

    def test_unknown_name_does_not_emit_deprecation_warning(
        self, _isolate_tasks_package
    ):
        """An unknown name must short-circuit *before* the warning."""
        tasks = _isolate_tasks_package
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(AttributeError):
                _ = tasks.some_other_unknown

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecations == []

    @pytest.mark.parametrize(
        "name", ["Broker", "BROKER", "run_backtest", "tasks", ""]
    )
    def test_case_and_typo_names_rejected(self, _isolate_tasks_package, name):
        tasks = _isolate_tasks_package
        with pytest.raises(AttributeError):
            getattr(tasks, name)


class TestDir:
    """Cover line 62: ``__dir__`` advertises the facade surface."""

    def test_dir_returns_all_facade_names(self, _isolate_tasks_package):
        tasks = _isolate_tasks_package
        assert set(dir(tasks)) == {"broker", "run_backtest_task", "scheduler"}

    def test_dir_result_is_sorted(self, _isolate_tasks_package):
        """``__dir__`` explicitly returns ``sorted(...)`` — pin that contract."""
        tasks = _isolate_tasks_package
        result = dir(tasks)
        assert result == sorted(result)
        # And the exact expected ordering:
        assert result == ["broker", "run_backtest_task", "scheduler"]

    def test_dir_returns_list_type(self, _isolate_tasks_package):
        tasks = _isolate_tasks_package
        result = dir(tasks)
        assert isinstance(result, list)


class TestSubmoduleImportsAreSilent:
    """Guard the documented invariant: importing submodules must NOT warn."""

    def test_submodule_import_does_not_trigger_facade_getattr(
        self, _isolate_tasks_package
    ):
        """``from engine.tasks import broker`` resolves via normal import
        machinery and never reaches ``__getattr__`` — so no warning."""
        # Drop our patched worker so this is a real (mocked-primitives) import.
        sys.modules.pop("engine.tasks.worker", None)
        sys.modules.pop("engine.tasks.broker", None)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            import importlib

            broker_mod = importlib.import_module("engine.tasks.broker")

        assert broker_mod is not None
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecations == []
