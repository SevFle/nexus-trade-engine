"""Execution backend factory and registry.

Provides a single entry point for creating execution backends by name,
driven by configuration. The registry can be extended with custom backends
at startup time.
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

from engine.core.execution.base import ExecutionBackend
from engine.core.execution.paper import PaperExecutionBackend, SlippageModel

logger = structlog.get_logger()

_REGISTRY: dict[str, type[ExecutionBackend]] = {}
_LOCK = threading.Lock()

_BUILTIN_BACKENDS: dict[str, type[ExecutionBackend]] = {}


def _ensure_builtins_loaded() -> None:
    if _BUILTIN_BACKENDS:
        return
    from engine.core.execution.backtest import BacktestBackend
    from engine.core.execution.live import LiveBackend

    _BUILTIN_BACKENDS["backtest"] = BacktestBackend
    _BUILTIN_BACKENDS["paper"] = PaperExecutionBackend
    _BUILTIN_BACKENDS["live"] = LiveBackend


def register_backend(name: str, backend_cls: type[ExecutionBackend]) -> None:
    if not name or not name.strip():
        raise ValueError("backend name must be non-empty")
    name = name.lower().strip()
    if not issubclass(backend_cls, ExecutionBackend):
        raise TypeError(
            f"{backend_cls.__name__} must be a subclass of ExecutionBackend"
        )
    with _LOCK:
        _REGISTRY[name] = backend_cls
    logger.info("execution.backend_registered", name=name, cls=backend_cls.__name__)


def list_backends() -> list[str]:
    _ensure_builtins_loaded()
    with _LOCK:
        return sorted(set(_BUILTIN_BACKENDS.keys()) | set(_REGISTRY.keys()))


def create_backend(
    name: str,
    **kwargs: Any,
) -> ExecutionBackend:
    """Create an execution backend by name with optional configuration.

    Supported names and their kwargs:
      - "backtest": fill_probability, partial_fill_enabled, random_seed
      - "paper": fill_probability, slippage_model, slippage_bps,
                 slippage_fixed_amount, slippage_jitter_range,
                 partial_fill_enabled, partial_fill_min_ratio,
                 partial_fill_volume_threshold, latency_ms_mean,
                 latency_ms_std, random_seed, price_provider, metrics
      - "live": broker_name, api_key, api_secret, base_url
      - Any custom name registered via register_backend()

    Args:
        name: Backend identifier (case-insensitive).
        **kwargs: Backend-specific configuration.

    Returns:
        Configured ExecutionBackend instance.

    Raises:
        ValueError: If the backend name is not recognized.
    """
    name = name.lower().strip()
    _ensure_builtins_loaded()

    with _LOCK:
        cls = _REGISTRY.get(name) or _BUILTIN_BACKENDS.get(name)

    if cls is None:
        available = sorted(set(_BUILTIN_BACKENDS.keys()) | set(_REGISTRY.keys()))
        raise ValueError(
            f"Unknown execution backend: {name!r}. "
            f"Available: {available}"
        )

    backend = _construct_backend(cls, name, **kwargs)
    logger.info(
        "execution.backend_created",
        name=name,
        cls=cls.__name__,
    )
    return backend


def _construct_backend(
    cls: type[ExecutionBackend],
    name: str,
    **kwargs: Any,
) -> ExecutionBackend:
    if name == "paper":
        return _construct_paper_backend(**kwargs)
    if name == "backtest":
        valid_keys = {"fill_probability", "partial_fill_enabled", "random_seed"}
        filtered = {k: v for k, v in kwargs.items() if k in valid_keys}
        return cls(**filtered)
    if name == "live":
        valid_keys = {"broker_name", "api_key", "api_secret", "base_url"}
        filtered = {k: v for k, v in kwargs.items() if k in valid_keys}
        return cls(**filtered)
    return cls(**kwargs)


def _construct_paper_backend(**kwargs: Any) -> PaperExecutionBackend:
    slippage_model = kwargs.get("slippage_model", SlippageModel.RANDOM)
    if isinstance(slippage_model, str):
        slippage_model = SlippageModel(slippage_model)

    return PaperExecutionBackend(
        fill_probability=kwargs.get("fill_probability", 0.95),
        slippage_model=slippage_model,
        slippage_bps=kwargs.get("slippage_bps", 5.0),
        slippage_fixed_amount=kwargs.get("slippage_fixed_amount", 0.01),
        slippage_jitter_range=kwargs.get("slippage_jitter_range", 0.3),
        partial_fill_enabled=kwargs.get("partial_fill_enabled", True),
        partial_fill_min_ratio=kwargs.get("partial_fill_min_ratio", 0.8),
        partial_fill_volume_threshold=kwargs.get("partial_fill_volume_threshold", 500),
        latency_ms_mean=kwargs.get("latency_ms_mean", 50.0),
        latency_ms_std=kwargs.get("latency_ms_std", 20.0),
        random_seed=kwargs.get("random_seed"),
        price_provider=kwargs.get("price_provider"),
        metrics=kwargs.get("metrics"),
    )


def _reset_for_tests() -> None:
    with _LOCK:
        _REGISTRY.clear()
        _BUILTIN_BACKENDS.clear()
