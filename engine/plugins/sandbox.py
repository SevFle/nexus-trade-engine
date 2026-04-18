"""
Strategy Sandbox - isolated execution environment for plugins.

Enforces 5 security layers:
  Layer 1: Import restrictions (RestrictedImporter on sys.meta_path)
  Layer 2: Network whitelist (SandboxedHttpClient)
  Layer 3: Resource limits (RLIMIT_AS, RLIMIT_NOFILE)
  Layer 4: Filesystem isolation (temp working dir, sandboxed open())
  Layer 5: Process isolation (production target - subprocess/container)

For the MVP, Layers 1-4 run in-process.  Layer 5 documents the production
architecture where each strategy executes in its own subprocess or container
with OS-level isolation (seccomp, namespaces, cgroups).
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import os
import resource
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from engine.core.signal import Signal
from engine.plugins.restricted_importer import BLOCKED_MODULES, RestrictedImporter
from engine.plugins.sandboxed_http import SandboxedHttpClient

if TYPE_CHECKING:
    from engine.core.cost_model import ICostModel
    from engine.core.portfolio import PortfolioSnapshot
    from engine.plugins.manifest import StrategyManifest
    from engine.plugins.sdk import BaseStrategy

logger = structlog.get_logger()

_original_open = builtins.open

_PRESERVE_ROOTS = frozenset(["http", "urllib"])
_STRIP_ROOTS = frozenset(["httpcore"])


@dataclass
class SandboxMetrics:
    """Runtime metrics for a sandboxed strategy."""

    total_evaluations: int = 0
    total_signals_emitted: int = 0
    total_cpu_time_ms: float = 0.0
    avg_evaluation_ms: float = 0.0
    peak_memory_mb: float = 0.0
    errors: int = 0
    last_error: str | None = None
    api_calls: int = 0


class StrategySandbox:
    """
    Wraps a strategy instance with resource monitoring and enforcement.

    All strategy method calls go through the sandbox, which:
    - Layer 1: Blocks dangerous imports via RestrictedImporter
    - Layer 2: Restricts network access to manifest-declared endpoints
    - Layer 3: Enforces resource limits (memory, file descriptors)
    - Layer 4: Isolates filesystem access to a temp working directory
    - Layer 5 (production): subprocess/container isolation (future target)

    CPU time is enforced via asyncio.wait_for timeout.
    Errors are caught and logged without crashing the engine.
    """

    def __init__(self, strategy: BaseStrategy, manifest: StrategyManifest):
        self.strategy = strategy
        self.manifest = manifest
        self.metrics = SandboxMetrics()
        self._max_eval_seconds = manifest.resources.max_cpu_seconds

        self._importer = RestrictedImporter()
        self._importer.install()

        self._allowed_endpoints = list(manifest.network.allowed_endpoints)
        self._http_client = self._create_sandboxed_http_client()

        self._saved_limits: dict[int, tuple[int, int]] = {}
        self._set_resource_limits()

        self._work_dir: str = tempfile.mkdtemp(prefix="nexus_sandbox_")

    def _create_sandboxed_http_client(self) -> SandboxedHttpClient | None:
        if self._allowed_endpoints:
            return SandboxedHttpClient(self._allowed_endpoints)
        return None

    def _set_resource_limits(self) -> None:
        max_bytes = self._parse_memory(self.manifest.resources.max_memory)
        self._saved_limits[resource.RLIMIT_AS] = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (max_bytes, max_bytes))

        orig_nofile = resource.getrlimit(resource.RLIMIT_NOFILE)
        new_soft = min(64, orig_nofile[0])
        resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, orig_nofile[1]))
        self._saved_limits[resource.RLIMIT_NOFILE] = orig_nofile

    @staticmethod
    def _parse_memory(mem_str: str) -> int:
        units = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}
        upper = mem_str.upper()
        for suffix, factor in units.items():
            if upper.endswith(suffix):
                return int(mem_str[: -len(suffix)].strip()) * factor
        return int(mem_str)

    def restore_limits(self) -> None:
        for rlimit, (soft, hard) in self._saved_limits.items():
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(rlimit, (soft, hard))
        self._importer.uninstall()

    def _make_sandboxed_open(self, work_dir: str):
        def sandboxed_open(file, mode="r", *args, **kwargs):
            path_str = str(file)
            if os.path.isabs(path_str):
                resolved = os.path.realpath(path_str)
            else:
                resolved = os.path.realpath(os.path.join(work_dir, path_str))

            work_dir_real = os.path.realpath(work_dir)
            if not (resolved == work_dir_real or resolved.startswith(work_dir_real + os.sep)):
                raise PermissionError(
                    f"File access to '{file}' is not allowed in strategy sandbox"
                )
            if any(c in mode for c in "wa+"):
                raise PermissionError("Write access is not allowed in strategy sandbox")
            return _original_open(resolved, mode, *args, **kwargs)

        return sandboxed_open

    async def safe_evaluate(
        self,
        portfolio: PortfolioSnapshot,
        market: Any,
        costs: ICostModel,  # noqa: ARG002
    ) -> list[Signal]:
        start = time.monotonic()

        eval_dir = self._work_dir

        removed_modules: dict[str, Any] = {}
        for name in list(sys.modules.keys()):
            root = name.split(".")[0]
            if (root in BLOCKED_MODULES and root not in _PRESERVE_ROOTS) or root in _STRIP_ROOTS:
                removed_modules[name] = sys.modules.pop(name)

        builtins.open = self._make_sandboxed_open(eval_dir)

        try:
            raw_signals = await asyncio.wait_for(
                self._call_strategy(portfolio, market),
                timeout=self._max_eval_seconds,
            )
        except TimeoutError:
            elapsed_ms = (time.monotonic() - start) * 1000
            self.metrics.errors += 1
            self.metrics.last_error = f"Timeout after {self._max_eval_seconds}s"
            logger.exception(
                "sandbox.timeout",
                strategy_name=self.strategy.name,
                timeout_s=self._max_eval_seconds,
            )
            return []

        except Exception as e:
            elapsed_ms = (time.monotonic() - start) * 1000
            self.metrics.errors += 1
            self.metrics.last_error = str(e)
            logger.exception(
                "sandbox.evaluation_error",
                strategy_name=self.strategy.name,
                error=str(e),
                elapsed_ms=elapsed_ms,
            )
            return []

        else:
            elapsed_ms = (time.monotonic() - start) * 1000
            signals = self._convert_signals(raw_signals)
            self._update_metrics(elapsed_ms, len(signals))
            return signals

        finally:
            builtins.open = _original_open
            sys.modules.update(removed_modules)
            shutil.rmtree(eval_dir, ignore_errors=True)
            self._work_dir = tempfile.mkdtemp(prefix="nexus_sandbox_")

    async def _call_strategy(
        self,
        portfolio: PortfolioSnapshot,
        market: Any,
    ) -> list[Any]:
        result = self.strategy.on_bar(market, portfolio)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    def _convert_signals(self, raw_signals: list[Any]) -> list[Signal]:
        validated: list[Signal] = []
        for s in raw_signals:
            if isinstance(s, Signal):
                if not s.strategy_id:
                    s.strategy_id = self.strategy.name
                validated.append(s)
            else:
                logger.warning(
                    "sandbox.invalid_signal",
                    strategy_name=self.strategy.name,
                    signal_type=type(s).__name__,
                )
        return validated

    def _update_metrics(self, elapsed_ms: float, signal_count: int):
        self.metrics.total_evaluations += 1
        self.metrics.total_signals_emitted += signal_count
        self.metrics.total_cpu_time_ms += elapsed_ms
        self.metrics.avg_evaluation_ms = (
            self.metrics.total_cpu_time_ms / self.metrics.total_evaluations
        )

    def get_health(self) -> dict:
        return {
            "strategy_name": self.strategy.name,
            "version": self.strategy.version,
            "evaluations": self.metrics.total_evaluations,
            "signals_emitted": self.metrics.total_signals_emitted,
            "avg_eval_ms": round(self.metrics.avg_evaluation_ms, 2),
            "errors": self.metrics.errors,
            "last_error": self.metrics.last_error,
        }
