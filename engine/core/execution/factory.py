"""
ExecutionBackendFactory — mode-based backend selection.

Creates the appropriate ExecutionBackend for backtest, paper_trade,
or live mode. Validates configuration and provides a singleton factory.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from engine.core.execution.backtest import BacktestBackend
from engine.core.execution.paper import PaperBackend, PaperTradeConfig
from engine.core.execution.paper_broker_interface import (
    PaperTradeBrokerConfig,
    PaperTradeRiskConfig,
)
from engine.core.execution.paper_trade_backend import PaperTradeExecutionBackend
from engine.core.execution.slippage import SlippageModelType

if TYPE_CHECKING:
    from engine.core.execution.base import ExecutionBackend

logger = structlog.get_logger()


class ExecutionMode(StrEnum):
    BACKTEST = "backtest"
    PAPER_TRADE = "paper_trade"
    LIVE = "live"


class ConfigurationError(Exception):
    pass


class BackendNotAvailableError(ConfigurationError):
    pass


def _validate_backtest_config(config: dict[str, Any]) -> None:
    if "random_seed" in config and not isinstance(config["random_seed"], (int, type(None))):
        raise ConfigurationError("random_seed must be int or None")
    if "fill_probability" in config:
        fp = config["fill_probability"]
        if not isinstance(fp, (int, float)) or not (0.0 <= fp <= 1.0):
            raise ConfigurationError("fill_probability must be between 0.0 and 1.0")


def _validate_paper_config(config: dict[str, Any]) -> None:
    if "fill_probability" in config:
        fp = config["fill_probability"]
        if not isinstance(fp, (int, float)) or not (0.0 <= fp <= 1.0):
            raise ConfigurationError("fill_probability must be between 0.0 and 1.0")
    if "latency_ms" in config:
        lm = config["latency_ms"]
        if not isinstance(lm, (int, float)) or lm < 0:
            raise ConfigurationError("latency_ms must be non-negative")
    if "slippage_model_type" in config:
        try:
            SlippageModelType(config["slippage_model_type"])
        except ValueError as exc:
            raise ConfigurationError(
                f"Invalid slippage_model_type: {config['slippage_model_type']}"
            ) from exc


class ExecutionBackendFactory:
    _instance: ExecutionBackendFactory | None = None

    def __init__(self) -> None:
        self._registry: dict[ExecutionMode, type[ExecutionBackend]] = {
            ExecutionMode.BACKTEST: BacktestBackend,
            ExecutionMode.PAPER_TRADE: PaperBackend,
        }

    @classmethod
    def get_instance(cls) -> ExecutionBackendFactory:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def create_backend(
        self,
        mode: ExecutionMode | str,
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ExecutionBackend:
        if isinstance(mode, str):
            try:
                mode = ExecutionMode(mode)
            except ValueError as exc:
                raise ConfigurationError(
                    f"Invalid execution mode: {mode!r}. "
                    f"Valid modes: {[m.value for m in ExecutionMode]}"
                ) from exc

        if mode == ExecutionMode.LIVE:
            raise BackendNotAvailableError(
                "Live execution backend is not yet implemented. "
                "Use 'backtest' or 'paper_trade' mode."
            )

        if mode not in self._registry:
            raise ConfigurationError(f"No backend registered for mode: {mode}")

        config = config or {}

        backend_cls = self._registry[mode]

        if backend_cls is BacktestBackend:
            _validate_backtest_config(config)
            return self._create_backtest(config, **kwargs)

        if backend_cls is PaperBackend:
            _validate_paper_config(config)
            use_full = config.get("use_full_backend", kwargs.get("use_full_backend", False))
            if use_full:
                return self._create_paper_trade_backend(config, **kwargs)
            return self._create_paper(config, **kwargs)

        return backend_cls(**config, **kwargs)

    def _create_backtest(
        self, config: dict[str, Any], **_kwargs: Any
    ) -> BacktestBackend:
        return BacktestBackend(
            fill_probability=config.get("fill_probability", 0.98),
            partial_fill_enabled=config.get("partial_fill_enabled", True),
            random_seed=config.get("random_seed"),
        )

    def _create_paper(
        self, config: dict[str, Any], **kwargs: Any
    ) -> PaperBackend:
        slippage_type = config.get("slippage_model_type", SlippageModelType.FIXED_BPS)
        if isinstance(slippage_type, str):
            slippage_type = SlippageModelType(slippage_type)

        paper_config = PaperTradeConfig(
            fill_probability=config.get("fill_probability", 0.95),
            partial_fill_enabled=config.get("partial_fill_enabled", True),
            partial_fill_min_ratio=config.get("partial_fill_min_ratio", 0.5),
            latency_ms=config.get("latency_ms", 50.0),
            latency_jitter_ms=config.get("latency_jitter_ms", 20.0),
            random_seed=config.get("random_seed"),
            slippage_model_type=slippage_type,
            slippage_model_kwargs=config.get("slippage_model_kwargs", {}),
            refresh_price_from_provider=config.get("refresh_price_from_provider", True),
        )
        return PaperBackend(
            config=paper_config,
            data_provider=kwargs.get("data_provider"),
        )

    def _create_paper_trade_backend(
        self, config: dict[str, Any], **kwargs: Any
    ) -> PaperTradeExecutionBackend:
        slippage_type = config.get("slippage_model_type", SlippageModelType.FIXED_BPS)
        if isinstance(slippage_type, str):
            slippage_type = SlippageModelType(slippage_type)

        risk_config = None
        if "risk_config" in config:
            rc = config["risk_config"]
            risk_config = PaperTradeRiskConfig(**rc)

        broker_config = PaperTradeBrokerConfig(
            fill_probability=config.get("fill_probability", 0.95),
            partial_fill_enabled=config.get("partial_fill_enabled", True),
            partial_fill_min_ratio=config.get("partial_fill_min_ratio", 0.5),
            latency_ms=config.get("latency_ms", 50.0),
            latency_jitter_ms=config.get("latency_jitter_ms", 20.0),
            random_seed=config.get("random_seed"),
            slippage_model_type=slippage_type,
            slippage_model_kwargs=config.get("slippage_model_kwargs", {}),
            commission_per_share=config.get("commission_per_share", 0.005),
            min_commission=config.get("min_commission", 1.0),
            refresh_price_from_provider=config.get("refresh_price_from_provider", True),
            risk_config=risk_config,
        )

        return PaperTradeExecutionBackend(
            config=broker_config,
            initial_cash=config.get("initial_cash", 100_000.0),
            data_provider=kwargs.get("data_provider"),
            commission_calculator=kwargs.get("commission_calculator"),
            event_bus=kwargs.get("event_bus"),
            clock=kwargs.get("clock"),
            metrics=kwargs.get("metrics"),
        )

    def register_backend(
        self, mode: ExecutionMode, backend_cls: type[ExecutionBackend]
    ) -> None:
        self._registry[mode] = backend_cls
        logger.info("factory.backend_registered", mode=mode.value, backend=backend_cls.__name__)


def create_execution_backend(
    mode: ExecutionMode | str,
    config: dict[str, Any] | None = None,
    **kwargs: Any,
) -> ExecutionBackend:
    factory = ExecutionBackendFactory.get_instance()
    return factory.create_backend(mode, config, **kwargs)
