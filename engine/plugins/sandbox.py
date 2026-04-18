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
import io as _io_module
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx as _httpx_module
import structlog

from engine.core.signal import Signal
from engine.plugins.restricted_importer import RestrictedImporter
from engine.plugins.sandboxed_http import SandboxedHttpClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from engine.core.cost_model import ICostModel
    from engine.core.portfolio import PortfolioSnapshot
    from engine.plugins.manifest import StrategyManifest

try:
    import resource as _resource

    HAS_RESOURCE_MODULE = True
except ImportError:
    _resource = None  # type: ignore[assignment]
    HAS_RESOURCE_MODULE = False

logger = structlog.get_logger()

_BLOCKED_ATTRS: frozenset[str] = frozenset(
    {
        "__subclasses__",
        "__bases__",
        "__mro__",
        "__globals__",
        "__closure__",
        "__code__",
    }
)

_eval_lock: asyncio.Lock = asyncio.Lock()


class _RestrictedObject:
    """Proxy for ``builtins.object`` that blocks ``__subclasses__()``."""

    @classmethod
    def __subclasses__(cls) -> list[type]:
        raise RuntimeError("__subclasses__() is not allowed in strategy sandbox")


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


class _PlaceholderStrategy:
    """Minimal stand-in used by ``from_factory`` during initialisation."""

    name = "_placeholder"
    version = "0.0.0"

    def on_bar(self, _state: Any, _portfolio: Any) -> list[Any]:
        return []


class StrategySandbox:
    """
    Wraps a strategy instance with resource monitoring and enforcement.

    All strategy method calls go through the sandbox, which:
    - Restricts imports to a safe allowlist
    - Whitelists network endpoints from the manifest
    - Enforces resource limits (memory, file descriptors, CPU timeout)
    - Isolates filesystem access to a temp directory
    - Blocks dangerous introspection (``__subclasses__``, ``__globals__``, etc.)
    - Serialises concurrent evaluations to prevent global-state races
    - Tracks metrics for the dashboard
    """

    def __init__(self, strategy: Any, manifest: StrategyManifest) -> None:
        self.strategy = strategy
        self.manifest = manifest
        self.metrics = SandboxMetrics()
        self._max_eval_seconds = manifest.resources.max_cpu_seconds

        self._importer = RestrictedImporter()
        self._http_client: SandboxedHttpClient | None = None
        self._work_dir: str | None = None
        self._original_open: Any = None
        self._saved_resource_limits: dict[str, tuple[int, int]] = {}

        self._original_getattr: Callable[..., Any] | None = None
        self._original_io_open: Any = None
        self._original_httpx_send: Any = None
        self._original_object: Any = None

        self._create_sandboxed_http_client()
        self._setup_filesystem_isolation()

    @classmethod
    def from_factory(
        cls,
        strategy_factory: Callable[[], Any],
        manifest: StrategyManifest,
    ) -> StrategySandbox:
        """
        Create a sandbox with restrictions active during strategy instantiation.

        Use this instead of the regular constructor to prevent C-2 bypass
        (strategy stashing module references in ``__init__``).
        """
        sandbox = cls(_PlaceholderStrategy(), manifest)
        sandbox._activate_restrictions()
        try:
            sandbox.strategy = strategy_factory()
        finally:
            sandbox._deactivate_restrictions()
        return sandbox

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
        allowed.extend(os.path.realpath(a) + os.sep for a in self.manifest.artifacts)
        allowed.extend(os.path.realpath(a) for a in self.manifest.artifacts)

        if not any(resolved == p or resolved.startswith(p + os.sep) for p in allowed if p):
            raise PermissionError(f"File access to {file} is not allowed in strategy sandbox")

        if any(c in mode for c in ("w", "a", "+")):
            raise PermissionError("Write access is not allowed in strategy sandbox")

        return self._original_open(file, mode, *args, **kwargs)

    def _restricted_getattr(self, obj: Any, name: str, *default: Any) -> Any:
        if name in _BLOCKED_ATTRS:
            raise PermissionError(f"Attribute '{name}' is not accessible in strategy sandbox")
        return self._original_getattr(obj, name, *default)  # type: ignore[misc]

    def _make_restricted_send(self) -> Any:
        allowed = self.manifest.network.allowed_endpoints
        original_send = self._original_httpx_send

        async def restricted_send(
            client: Any,
            request: Any,
            *,
            stream: bool = False,
            **kwargs: Any,
        ) -> Any:
            host = request.url.host
            if not any(host == ep or host.endswith(f".{ep}") for ep in allowed):
                raise PermissionError(f"Network access to {host} is not allowed")
            return await original_send(client, request, stream=stream, **kwargs)

        return restricted_send

    def _activate_restrictions(self) -> None:
        self._importer.install()
        self._original_object = builtins.object
        builtins.object = _RestrictedObject  # type: ignore[assignment]
        self._original_getattr = builtins.getattr
        builtins.getattr = self._restricted_getattr  # type: ignore[assignment]
        self._original_io_open = _io_module.open
        _io_module.open = self._restricted_open  # type: ignore[assignment]
        self._original_httpx_send = _httpx_module.AsyncClient.send
        _httpx_module.AsyncClient.send = self._make_restricted_send()
        self._original_open = builtins.open
        builtins.open = self._restricted_open  # type: ignore[assignment]
        self._apply_resource_limits()

    def _deactivate_restrictions(self) -> None:
        self._importer.uninstall()
        if self._original_object is not None:
            builtins.object = self._original_object
            self._original_object = None
        if self._original_getattr is not None:
            builtins.getattr = self._original_getattr  # type: ignore[assignment]
            self._original_getattr = None
        if self._original_io_open is not None:
            _io_module.open = self._original_io_open
            self._original_io_open = None
        if self._original_httpx_send is not None:
            _httpx_module.AsyncClient.send = self._original_httpx_send
            self._original_httpx_send = None
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

        Serialised via ``_eval_lock`` to prevent concurrent sandboxes
        from corrupting each other's global builtin patches (C-4).
        Returns empty list on failure - never crashes the engine.
        """
        async with _eval_lock:
            return await self._evaluate_inner(portfolio, market)

    async def _evaluate_inner(
        self,
        portfolio: PortfolioSnapshot,
        market: Any,
    ) -> list[Signal]:
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
