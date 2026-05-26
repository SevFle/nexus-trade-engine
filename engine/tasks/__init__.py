"""Re-export facade for engine.tasks.worker.

Importing from ``engine.tasks`` is deprecated.  Use
``engine.tasks.worker`` directly instead.
"""

from __future__ import annotations

import warnings

from engine.tasks.worker import broker, run_backtest_task, scheduler

warnings.warn(
    "Importing from 'engine.tasks' is deprecated; "
    "import from 'engine.tasks.worker' instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["broker", "run_backtest_task", "scheduler"]
