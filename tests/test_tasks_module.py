"""Tests for engine.tasks package — validates module structure and imports."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


class TestTasksModule:
    @pytest.fixture(autouse=True)
    def _mock_redis_deps(self):
        mock_broker_inst = MagicMock()
        mock_broker_inst.with_result_backend.return_value = mock_broker_inst
        mock_broker_inst.with_middlewares.return_value = mock_broker_inst
        mock_broker_inst.task = lambda: lambda f: f

        mods_to_remove = [k for k in sys.modules if k.startswith("engine.tasks")]
        saved = {m: sys.modules.pop(m) for m in mods_to_remove}

        with (
            patch("engine.tasks.broker.ListQueueBroker", return_value=mock_broker_inst),
            patch("engine.tasks.broker.RedisAsyncResultBackend"),
            patch("engine.tasks.broker.CorrelationMiddleware"),
            patch("engine.tasks.broker.TaskiqScheduler"),
        ):
            yield

        for m in mods_to_remove:
            sys.modules.pop(m, None)
        sys.modules.update(saved)

    def test_worker_module_importable(self):
        from engine.tasks import worker

        assert hasattr(worker, "broker")
        assert hasattr(worker, "run_backtest_task")

    def test_tasks_package_importable(self):
        import engine.tasks

        assert engine.tasks is not None

    def test_worker_has_scheduler(self):
        from engine.tasks import worker

        assert hasattr(worker, "scheduler")

    def test_dir_advertises_deprecated_facade_names(self):
        import engine.tasks

        advertised = dir(engine.tasks)
        # The deprecated facade surface is exactly the historical names
        # that were re-exported from engine.tasks.worker.
        assert set(advertised) == {"broker", "run_backtest_task", "scheduler"}

    @pytest.mark.parametrize(
        "facade_name", ["run_backtest_task", "scheduler"]
    )
    def test_deprecated_facade_access_warns_and_delegates(self, facade_name):
        """Accessing a historical facade name on ``engine.tasks`` emits a
        ``DeprecationWarning`` and lazily delegates to
        :mod:`engine.tasks.worker` (covering ``__getattr__``'s facade
        branch — lines 43-52 of ``engine/tasks/__init__.py``).

        ``broker`` is intentionally excluded: it shares its name with the
        ``engine.tasks.broker`` submodule, so once that submodule is
        imported (as it is by the app factory and by this test's patches)
        it binds directly to the package ``__dict__`` and the lazy
        ``__getattr__`` is bypassed — the documented, expected behaviour
        for the one facade name that collides with a submodule.
        """
        import engine.tasks
        from engine.tasks import worker

        with pytest.warns(DeprecationWarning, match="import from 'engine.tasks.worker'"):
            value = getattr(engine.tasks, facade_name)

        assert value is getattr(worker, facade_name)

    def test_unknown_attribute_raises_attribute_error(self):
        """Accessing a name that is neither a facade attribute nor a real
        submodule raises ``AttributeError`` (covering ``__getattr__``'s
        final raise — line 62 of ``engine/tasks/__init__.py``)."""
        import engine.tasks

        with pytest.raises(AttributeError, match="has no attribute 'does_not_exist'"):
            _ = engine.tasks.does_not_exist

    def test_submodule_import_is_silent(self):
        """Submodule imports must NOT trip the deprecation warning (the
        facade defers only to attribute access, per the module docstring)."""
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            # Re-importing the submodule directly should not raise.
            import importlib

            importlib.import_module("engine.tasks.broker")
            importlib.import_module("engine.tasks.worker")
