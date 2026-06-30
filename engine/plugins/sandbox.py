"""
Strategy Sandbox - isolated execution environment for plugins.

Enforces five security layers:
  1. Import restrictions (**allowlist** model via RestrictedImporter)
  2. Network whitelist (SandboxedHttpClient for declared endpoints)
  3. Resource limits (memory, file descriptors via resource on Linux)
  4. Filesystem isolation (temp working dir, read-only artifacts)
  5. Process isolation (subprocess/container - production target, not yet implemented)

For the current MVP, layers 1-4 provide in-process isolation.  Layer 5
is the production architecture where each strategy runs in its own
process or container, communicated with via pipes (serialized
MarketState in, Signal[] out), and killed if it exceeds limits.

Security note
-------------
This module deliberately keeps **no module-level references** to dangerous
modules (``os``, ``io``, ``shutil``, ``httpx``, ``resource``, ``asyncio``,
``builtins``).  Those imports are performed *inside* the methods that need them
(while restrictions are *not* yet active, or after they have been lifted) and
the resulting bindings are local, so sandboxed code that reaches this module's
globals via ``engine.plugins.sandbox`` cannot discover them.  A handful of
captured *callables/values* (``_realpath``, ``_sep``, ``_eval_lock``,
``_wait_for``, ``_iscoroutine``) are retained because they are needed while
restrictions are active; none of them exposes a dangerous module object.

The previous ``ContextVar``-based security gate has been replaced with a
process-level flag (see :class:`_ProcessSandboxFlag`) because a ``ContextVar``
is process-wide mutable state that attacker code could clear via the
``contextvars`` module — which is now itself blocked by the allowlist.  The flag
is additionally unreachable by sandboxed code because importing
``engine.plugins.sandbox`` is denied by :data:`_DENIED_SUBMODULES` in the
restricted importer.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from engine.core.signal import Signal
from engine.plugins.restricted_importer import RestrictedImporter
from engine.plugins.sandboxed_http import SandboxedHttpClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from engine.core.cost_model import ICostModel
    from engine.core.portfolio import PortfolioSnapshot
    from engine.plugins.manifest import StrategyManifest


# Capture path-resolution helpers from ``os`` WITHOUT retaining a module-level
# reference to the (blocked) ``os`` module.  ``_realpath`` is a plain function
# and ``_sep`` a string constant; neither exposes the ``os`` module nor
# constitutes an escape vector on its own.  They are needed by
# ``_restricted_open`` which executes while import restrictions are active (so
# ``import os`` would be rejected by the allowlist).
def _capture_os_path_helpers() -> tuple[Any, str]:
    import os

    return os.path.realpath, os.sep


_realpath, _sep = _capture_os_path_helpers()


# ``resource`` is probed at import time without retaining the module object, so
# that no module-level reference to this blocked module exists.  The actual
# module reference is captured per-instance in ``StrategySandbox.__init__``.
def _detect_resource_module() -> bool:
    try:
        import resource  # noqa: F401 -- probe only, reference not retained
    except ImportError:
        return False
    return True


HAS_RESOURCE_MODULE: bool = _detect_resource_module()


# Capture asyncio primitives without retaining the (blocked) ``asyncio`` module
# at module level, so sandboxed code cannot reach it via
# ``engine.plugins.sandbox.asyncio``.  The captured lock/functions carry their
# own internal state and do not expose the module object.
def _capture_asyncio_helpers() -> tuple[Any, Any, Any]:
    import asyncio

    return asyncio.Lock(), asyncio.wait_for, asyncio.iscoroutine


_eval_lock, _wait_for, _iscoroutine = _capture_asyncio_helpers()

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


class _ProcessSandboxFlag:
    """Process-level sandbox-active flag.

    Replaces the former ``contextvars.ContextVar`` security gate.

    A ``ContextVar`` is process-wide mutable state that attacker code could
    trivially clear by importing ``contextvars`` and manipulating the context
    machinery (``copy_context``, ``Token.reset``, etc.).  This class is a plain
    boolean with a deliberately small, compatible API (``get``/``set``/``name``)
    so that existing call-sites and tests continue to work, while the
    ``contextvars`` module itself is blocked by the import allowlist — closing
    the reset/clear escape vector.

    In the production subprocess-isolation architecture (Layer 5) this flag
    becomes implicit: the child process *is* the sandbox and there is no shared
    state to manipulate.
    """

    name = "_in_sandbox_execution"

    def __init__(self) -> None:
        self._value: bool = False

    def get(self, default: bool = False) -> bool:  # noqa: ARG002
        return self._value

    def set(self, value: bool) -> None:
        self._value = value

    def reset(self) -> None:
        self._value = False


_in_sandbox_execution: _ProcessSandboxFlag = _ProcessSandboxFlag()


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

        # Tracks whether ``_restricted_getattr`` already recorded a blocked
        # attribute violation during the current evaluation, so that
        # ``_evaluate_inner`` doesn't double-count the propagating exception.
        # Reset at the start of every ``_evaluate_inner`` call.
        self._getattr_violation_counted: bool = False

        # Capture the ``resource`` module reference now (before any restrictions
        # are active) and store it on the instance.  Strategy code never
        # receives a reference to the sandbox object, so this is unreachable,
        # and there is no module-level ``resource`` reference for sandboxed code
        # to discover via ``engine.plugins.sandbox``.
        self._resource_module: Any = None
        if HAS_RESOURCE_MODULE:
            try:
                import resource as _resource_mod
            except ImportError:
                self._resource_module = None
            else:
                self._resource_module = _resource_mod

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
        _resource = self._resource_module
        if _resource is None:
            return

        try:
            max_bytes = self._parse_memory(self.manifest.resources.max_memory)
            soft, hard = _resource.getrlimit(_resource.RLIMIT_AS)
            new_soft = min(max_bytes, hard)
            _resource.setrlimit(_resource.RLIMIT_AS, (new_soft, hard))
            self._saved_resource_limits["RLIMIT_AS"] = (soft, hard)
        except (ValueError, OSError, AttributeError):
            pass

        try:
            soft, hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
            new_soft = min(64, hard)
            _resource.setrlimit(_resource.RLIMIT_NOFILE, (new_soft, hard))
            self._saved_resource_limits["RLIMIT_NOFILE"] = (soft, hard)
        except (ValueError, OSError, AttributeError):
            pass

    def _restore_resource_limits(self) -> None:
        _resource = self._resource_module
        if _resource is None:
            return

        for name, (soft, hard) in self._saved_resource_limits.items():
            with contextlib.suppress(ValueError, OSError, AttributeError):
                _resource.setrlimit(
                    getattr(_resource, name),
                    (soft, hard),
                )
        self._saved_resource_limits.clear()

    def _setup_filesystem_isolation(self) -> None:
        import tempfile

        self._work_dir = tempfile.mkdtemp(prefix="strategy_sandbox_")

    def _restricted_open(
        self,
        file: Any,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if not _in_sandbox_execution.get(False):
            return self._original_open(file, mode, *args, **kwargs)

        if isinstance(file, int):
            raise PermissionError("File descriptor access is not allowed in strategy sandbox")

        resolved = _realpath(str(file))
        work_dir = _realpath(self._work_dir or "")

        allowed = [work_dir]
        allowed.extend(_realpath(a) + _sep for a in self.manifest.artifacts)
        allowed.extend(_realpath(a) for a in self.manifest.artifacts)

        if not any(resolved == p or resolved.startswith(p + _sep) for p in allowed if p):
            raise PermissionError(f"File access to {file} is not allowed in strategy sandbox")

        if any(c in mode for c in ("w", "a", "+")):
            raise PermissionError("Write access is not allowed in strategy sandbox")

        return self._original_open(file, mode, *args, **kwargs)

    def _restricted_getattr(self, obj: Any, name: str, *default: Any) -> Any:
        if name in _BLOCKED_ATTRS:
            # Record the security violation *before* raising (or returning
            # the caller's default) so the attempt is visible in
            # ``metrics.errors`` / ``metrics.last_error`` even when the
            # strategy swallows the exception or uses the 3-argument
            # ``getattr(obj, name, default)`` form.  The flag lets
            # ``_evaluate_inner`` skip its own increment for the propagating
            # exception, avoiding a double count.
            violation_msg = f"Attribute '{name}' is not accessible in strategy sandbox"
            self.metrics.errors += 1
            self.metrics.last_error = violation_msg
            self._getattr_violation_counted = True
            if default:
                # Respect the 3-argument ``getattr(obj, name, default)`` contract.
                # A caller that supplies a default — notably
                # ``inspect.get_annotations`` and ``_signature_from_function``
                # which both call ``getattr(obj, '__globals__', None)`` —
                # expects the default value back and *never* an exception.
                # Returning the caller's default (rather than the real value)
                # keeps the blocked attribute's contents unreachable while
                # allowing legitimate introspection libraries (inspect,
                # dataclasses, pydantic) to function inside the sandbox.
                # Direct attacker access ``getattr(obj, '__globals__')`` (no
                # default) still raises ``PermissionError``.
                return default[0]
            # Tag the exception itself so ``_evaluate_inner`` can tell that
            # *this specific* propagating exception was already counted by us.
            # Relying solely on the instance-level ``_getattr_violation_counted``
            # flag would be wrong: if the strategy swallows the ``PermissionError``
            # and then raises a *different* error in the same evaluation, that
            # later error must still be counted (the flag would otherwise mask it).
            err = PermissionError(violation_msg)
            err._sandbox_violation_counted = True  # type: ignore[attr-defined]  # noqa: SLF001
            raise err
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
            if not _in_sandbox_execution.get(False):
                return await original_send(client, request, stream=stream, **kwargs)
            host = request.url.host
            if not any(host == ep or host.endswith(f".{ep}") for ep in allowed):
                raise PermissionError(f"Network access to {host} is not allowed")
            return await original_send(client, request, stream=stream, **kwargs)

        return restricted_send

    def _activate_restrictions(self) -> None:
        import builtins as _builtins
        import io as _io

        import httpx as _httpx

        self._importer.install()
        self._original_object = _builtins.object
        _builtins.object = _RestrictedObject  # type: ignore[assignment]
        self._original_getattr = _builtins.getattr
        _builtins.getattr = self._restricted_getattr  # type: ignore[assignment]
        self._original_io_open = _io.open
        _io.open = self._restricted_open  # type: ignore[assignment]
        self._original_httpx_send = _httpx.AsyncClient.send
        _httpx.AsyncClient.send = self._make_restricted_send()
        self._original_open = _builtins.open
        _builtins.open = self._restricted_open  # type: ignore[assignment]
        self._apply_resource_limits()
        _in_sandbox_execution.set(True)

    def _deactivate_restrictions(self) -> None:
        _in_sandbox_execution.set(False)
        self._importer.uninstall()
        import builtins as _builtins

        if self._original_object is not None:
            _builtins.object = self._original_object
            self._original_object = None
        if self._original_getattr is not None:
            _builtins.getattr = self._original_getattr  # type: ignore[assignment]
            self._original_getattr = None
        if self._original_io_open is not None:
            import io as _io

            _io.open = self._original_io_open
            self._original_io_open = None
        if self._original_httpx_send is not None:
            import httpx as _httpx

            _httpx.AsyncClient.send = self._original_httpx_send
            self._original_httpx_send = None
        if self._original_open is not None:
            _builtins.open = self._original_open  # type: ignore[assignment]
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
        self._getattr_violation_counted = False
        self._activate_restrictions()

        try:
            raw_signals = await _wait_for(
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
            # If *this* exception originated from ``_restricted_getattr`` it was
            # already recorded there; skip the double count.  The check is bound
            # to the exception object (not the instance flag) so that a
            # *different* error raised in the same evaluation — after a
            # swallowed getattr violation — is still counted.
            if not getattr(e, "_sandbox_violation_counted", False):
                self.metrics.errors += 1
                self.metrics.last_error = str(e)
            # Reset the per-evaluation flag so a subsequent call starts clean;
            # ``_evaluate_inner`` also resets it at entry as a belt-and-braces.
            self._getattr_violation_counted = False
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
        if _iscoroutine(result):
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
        self._deactivate_restrictions()
        if self._work_dir:
            import os
            import shutil

            if os.path.isdir(self._work_dir):
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
