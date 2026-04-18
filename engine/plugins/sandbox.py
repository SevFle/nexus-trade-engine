"""
Strategy Sandbox - isolated execution environment for plugins.

Enforces five security layers:
  1. Import restrictions (blocked modules via RestrictedImporter)
  2. Network whitelist (SandboxedHttpClient for declared endpoints)
  3. Resource limits (memory, file descriptors via resource on Linux)
  4. Filesystem isolation (temp working dir, read-only artifacts)
  5. Process isolation (subprocess/container - production target, not yet implemented)

For the current MVP, layers 1-4 provide in-process isolation.  Layer 5
is the production architecture where each strategy runs in its own
process or container, communicated with via pipes (serialized
MarketState in, Signal[] out), and killed if it exceeds limits.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import os
import shutil
import tempfile
import time
from collections.abc import Coroutine as _CoroutineType
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from engine.core.signal import Signal
from engine.plugins.restricted_importer import RestrictedImporter
from engine.plugins.sandboxed_http import SandboxedHttpClient

if TYPE_CHECKING:
    from engine.core.cost_model import ICostModel
    from engine.core.portfolio import PortfolioSnapshot
    from engine.plugins.manifest import StrategyManifest
    from engine.plugins.sdk import BaseStrategy

try:
    import resource as _resource

    HAS_RESOURCE_MODULE = True
except ImportError:
    _resource = None  # type: ignore[assignment]
    HAS_RESOURCE_MODULE = False

logger = structlog.get_logger()

_BLOCKED_BUILTINS: frozenset[str] = frozenset(
    [
        "getattr",
        "hasattr",
        "setattr",
        "delattr",
        "type",
        "dir",
        "vars",
        "exec",
        "eval",
        "compile",
        "breakpoint",
    ]
)


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
    - Restricts imports to a safe allowlist
    - Whitelists network endpoints from the manifest
    - Enforces resource limits (memory, file descriptors, CPU timeout)
    - Isolates filesystem access to a temp directory
    - Tracks metrics for the dashboard
    """

    def __init__(self, strategy: BaseStrategy, manifest: StrategyManifest) -> None:
        self.strategy = strategy
        self.manifest = manifest
        self.metrics = SandboxMetrics()
        self._max_eval_seconds = manifest.resources.max_cpu_seconds

        self._importer = RestrictedImporter()
        self._http_client: SandboxedHttpClient | None = None
        self._work_dir: str | None = None
        self._original_open: Any = None
        self._saved_resource_limits: dict[str, tuple[int, int]] = {}
        self._saved_builtins: dict[str, Any] = {}
        self._restore_setattr: Any = builtins.setattr

        self._create_sandboxed_http_client()
        self._setup_filesystem_isolation()

    def _create_sandboxed_http_client(self) -> None:
        if self.manifest.requires_network():
            self._http_client = SandboxedHttpClient(
                allowed_endpoints=self.manifest.network.allowed_endpoints,
            )

    @staticmethod
    def _parse_memory(mem_str: str) -> int:
        val = mem_str.strip().upper()
        units: dict[str, int] = {
            "GB": 1024**3,
            "MB": 1024**2,
            "KB": 1024,
            "B": 1,
        }
        for suffix, multiplier in sorted(units.items(), key=lambda x: -len(x[0])):
            if val.endswith(suffix):
                return int(float(val[: -len(suffix)]) * multiplier)
        return int(val)

    def _apply_resource_limits(self) -> None:
        if not HAS_RESOURCE_MODULE:
            return

        try:
            max_bytes = self._parse_memory(self.manifest.resources.max_memory)
            soft, hard = _resource.getrlimit(_resource.RLIMIT_AS)  # type: ignore[union-attr]
            new_soft = min(max_bytes, hard)
            _resource.setrlimit(_resource.RLIMIT_AS, (new_soft, hard))  # type: ignore[union-attr]
            self._saved_resource_limits["RLIMIT_AS"] = (soft, hard)
        except (ValueError, OSError, AttributeError):
            pass

        try:
            soft, hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)  # type: ignore[union-attr]
            new_soft = min(64, hard)
            _resource.setrlimit(_resource.RLIMIT_NOFILE, (new_soft, hard))  # type: ignore[union-attr]
            self._saved_resource_limits["RLIMIT_NOFILE"] = (soft, hard)
        except (ValueError, OSError, AttributeError):
            pass

    def _restore_resource_limits(self) -> None:
        if not HAS_RESOURCE_MODULE:
            return

        for name, (soft, hard) in self._saved_resource_limits.items():
            with contextlib.suppress(ValueError, OSError, AttributeError):
                _resource.setrlimit(  # type: ignore[union-attr]
                    getattr(_resource, name),  # type: ignore[union-attr]
                    (soft, hard),
                )
        self._saved_resource_limits.clear()

    def _setup_filesystem_isolation(self) -> None:
        self._work_dir = tempfile.mkdtemp(prefix="strategy_sandbox_")

    def _restricted_open(
        self,
        file: Any,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if isinstance(file, int):
            raise PermissionError("File descriptor access is not allowed in strategy sandbox")

        resolved = os.path.realpath(str(file))
        work_dir = os.path.realpath(self._work_dir or "")

        allowed = [work_dir]
        allowed.extend(os.path.realpath(a) for a in self.manifest.artifacts)

        if not any(resolved.startswith(p) for p in allowed if p):
            raise PermissionError(f"File access to {file} is not allowed in strategy sandbox")

        if any(c in mode for c in ("w", "a", "+")):
            raise PermissionError("Write access is not allowed in strategy sandbox")

        return self._original_open(file, mode, *args, **kwargs)

    def _activate_restrictions(self) -> None:
        self._importer.install()
        self._original_open = builtins.open
        builtins.open = self._restricted_open  # type: ignore[assignment]
        self._apply_resource_limits()

    def _activate_builtin_restrictions(self) -> None:
        _delete = delattr
        self._saved_builtins = {}
        for name in _BLOCKED_BUILTINS:
            value = builtins.__dict__.get(name)
            if value is not None:
                self._saved_builtins[name] = value
        for name in self._saved_builtins:
            _delete(builtins, name)

    def _deactivate_builtin_restrictions(self) -> None:
        _sa = self._restore_setattr
        for name, value in self._saved_builtins.items():
            _sa(builtins, name, value)
        self._saved_builtins.clear()

    def _deactivate_restrictions(self) -> None:
        self._importer.uninstall()
        if self._original_open is not None:
            builtins.open = self._original_open  # type: ignore[assignment]
            self._original_open = None
        self._restore_resource_limits()

    async def safe_evaluate(
        self,
        portfolio: PortfolioSnapshot,
        market: Any,
        costs: ICostModel,  # noqa: ARG002
    ) -> list[Signal]:
        """
        Execute strategy.on_bar() with timeout and error handling.

        All security layers are active for the duration of the strategy
        call.  Returns empty list on failure - never crashes the engine.
        """
        start = time.monotonic()
        self._activate_restrictions()

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
        finally:
            self._deactivate_restrictions()

        elapsed_ms = (time.monotonic() - start) * 1000
        signals = self._convert_signals(raw_signals)
        self._update_metrics(elapsed_ms, len(signals))
        return signals

    async def _call_strategy(
        self,
        portfolio: PortfolioSnapshot,
        market: Any,
    ) -> list[Any]:
        self._activate_builtin_restrictions()
        try:
            result = self.strategy.on_bar(market, portfolio)
            if isinstance(result, _CoroutineType):
                result = await result
            return result  # type: ignore[return-value]
        finally:
            self._deactivate_builtin_restrictions()

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

    def _update_metrics(self, elapsed_ms: float, signal_count: int) -> None:
        self.metrics.total_evaluations += 1
        self.metrics.total_signals_emitted += signal_count
        self.metrics.total_cpu_time_ms += elapsed_ms
        self.metrics.avg_evaluation_ms = (
            self.metrics.total_cpu_time_ms / self.metrics.total_evaluations
        )

    def cleanup(self) -> None:
        """Release all sandbox resources (temp dir, hooks, HTTP client)."""
        self._importer.uninstall()
        if self._original_open is not None:
            builtins.open = self._original_open
            self._original_open = None

        if self._saved_builtins:
            _sa = self._restore_setattr
            for name, value in self._saved_builtins.items():
                _sa(builtins, name, value)
            self._saved_builtins.clear()

        self._restore_resource_limits()

        if self._work_dir and os.path.isdir(self._work_dir):
            shutil.rmtree(self._work_dir, ignore_errors=True)
            self._work_dir = None

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
