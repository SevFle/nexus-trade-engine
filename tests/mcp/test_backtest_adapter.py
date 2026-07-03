"""Unit tests for ``engine.mcp.adapters.backtest_adapter.run_backtest``.

The ``run_backtest`` MCP tool is the compute-intensive entry point of the
Nexus MCP server. It translates a tool call into a
:class:`~engine.core.backtest_runner.BacktestRunner` invocation and folds the
result into a compact, LLM-friendly JSON summary.

These tests pin the adapter contract **without** touching the network, a
database, or running a real backtest:

* argument validation (required fields + capital bounds),
* the strategy-not-found → ``NotFoundError`` mapping,
* the missing-provider → ``EngineError`` mapping,
* the runner-failure → ``EngineError`` normalisation,
* the happy-path summary shape (metrics filtering, evaluation extraction,
  ``equity_curve_truncated`` sentinel, NaN/inf sanitisation),
* progress-reporter lifecycle (start/complete on success, no complete on
  failure, safe when ``None``),
* ``BacktestConfig`` wiring (correct fields + default capital),
* the :func:`~engine.mcp.handlers.dispatch_tool` integration for the
  ``run_backtest`` route (including unknown-tool / missing-arg guards).

``BacktestRunner`` is mocked everywhere it is constructed, so the data
provider, plugin registry, and runner are all fakes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.core.backtest_runner import BacktestConfig, BacktestResult
from engine.mcp.adapters import EngineServices, backtest_adapter
from engine.mcp.adapters.backtest_adapter import run_backtest
from engine.mcp.auth import AuthPrincipal
from engine.mcp.config import mcp_settings
from engine.mcp.errors import EngineError, MCPError, NotFoundError, ValidationError
from engine.mcp.handlers import dispatch_tool

# ── A constant principal used by every test ────────────────────────────────
PRINCIPAL = AuthPrincipal(user_id="quant-1", role="quant_dev", auth_method="jwt")

# Minimal valid argument set; individual tests mutate copies of this.
BASE_ARGS: dict[str, Any] = {
    "strategy_name": "momentum",
    "symbol": "AAPL",
    "start_date": "2023-01-01",
    "end_date": "2023-06-30",
}


# ── Helpers / fixtures ──────────────────────────────────────────────────── #
class _FakeProgress:
    """Recording stand-in for :class:`~engine.mcp.progress.ProgressReporter`."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, float | None, str | None]] = []

    async def report(
        self,
        progress: float,
        *,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        self.calls.append((progress, total, message))


def _make_services(
    *,
    load_return: Any = "STRATEGY",
    provider: Any = object(),
    provider_factory: Any = None,
) -> EngineServices:
    """Build an :class:`EngineServices` with mocked, injectable parts.

    By default the plugin registry's ``load_strategy`` returns a truthy
    sentinel and the provider factory returns a non-None provider, which is
    exactly what the happy path needs. Tests for the not-found / no-provider
    branches override ``load_return`` / ``provider``.
    """
    registry = MagicMock()
    registry.load_strategy.return_value = load_return

    def _default_factory() -> Any:
        return provider

    factory = provider_factory if provider_factory is not None else _default_factory

    return EngineServices(
        plugin_registry=registry,
        market_data_provider_factory=factory,
    )


def _make_result(
    *,
    final_capital: float = 112_345.6789,
    total_return_pct: float = 12.34567,
    trades: list[dict[str, Any]] | None = None,
    equity_curve: list[dict[str, Any]] | None = None,
    metrics: dict[str, Any] | None = None,
) -> BacktestResult:
    """Construct a realistic ``BacktestResult`` for assertions."""
    return BacktestResult(
        final_capital=final_capital,
        total_return_pct=total_return_pct,
        trades=trades if trades is not None else [{"side": "buy"}, {"side": "sell"}],
        equity_curve=(
            equity_curve
            if equity_curve is not None
            else [{"timestamp": "t1"}, {"timestamp": "t2"}, {"timestamp": "t3"}]
        ),
        metrics=metrics if metrics is not None else {"sharpe": 1.5, "max_drawdown": -0.12},
    )


def _runner_patch(result: BacktestResult | None = None, *, raises: BaseException | None = None):
    """Patch ``BacktestRunner`` on the adapter module.

    Because ``patch.object(target, attr, new)`` yields ``new`` from its
    context manager, the bound name *is* the constructor spy. Thus::

        with _runner_patch(result=res) as ctor:
            summary = await run_backtest(...)
        ctor.assert_called_once()                       # was BacktestRunner(...)
        ctor.return_value.run.assert_awaited_once()     # the instance's run()
        cfg = ctor.call_args.kwargs["config"]           # the BacktestConfig

    ``ctor.return_value`` is the constructed runner instance.
    """
    instance = MagicMock(name="BacktestRunner_instance")
    instance.run = (
        AsyncMock(side_effect=raises) if raises is not None else AsyncMock(return_value=result)
    )
    return patch.object(backtest_adapter, "BacktestRunner", MagicMock(return_value=instance))


# ── 1. Argument validation ─────────────────────────────────────────────── #
@pytest.mark.parametrize(
    ("overrides", "expected_substring"),
    [
        ({"strategy_name": None}, "strategy_name is required"),
        ({"strategy_name": ""}, "strategy_name is required"),
        ({"symbol": None}, "symbol is required"),
        ({"symbol": ""}, "symbol is required"),
        ({"start_date": None}, "start_date and end_date are required"),
        ({"start_date": ""}, "start_date and end_date are required"),
        ({"end_date": None}, "start_date and end_date are required"),
        ({"end_date": ""}, "start_date and end_date are required"),
        ({"initial_capital": 0}, "initial_capital must be positive"),
        ({"initial_capital": -1}, "initial_capital must be positive"),
        ({"initial_capital": -1000.5}, "initial_capital must be positive"),
    ],
    ids=[
        "strategy_name-missing",
        "strategy_name-empty",
        "symbol-missing",
        "symbol-empty",
        "start_date-missing",
        "start_date-empty",
        "end_date-missing",
        "end_date-empty",
        "capital-zero",
        "capital-negative-int",
        "capital-negative-float",
    ],
)
async def test_run_backtest_rejects_invalid_arguments(overrides, expected_substring):
    """Each validation rule raises ``ValidationError`` before any I/O."""
    services = _make_services()
    args = {**BASE_ARGS, **overrides}

    with pytest.raises(ValidationError) as exc_info:
        await run_backtest(services, PRINCIPAL, args)

    assert expected_substring in str(exc_info.value)
    # Validation must short-circuit before the registry is consulted.
    services.plugin_registry.load_strategy.assert_not_called()


async def test_run_backtest_does_not_construct_runner_on_validation_failure():
    """A bad argument must never reach ``BacktestRunner``."""
    services = _make_services()
    with _runner_patch() as ctor, pytest.raises(ValidationError):
        await run_backtest(services, PRINCIPAL, {**BASE_ARGS, "symbol": ""})
    ctor.assert_not_called()


# ── 2. Strategy not found ───────────────────────────────────────────────── #
async def test_run_backtest_strategy_not_found_raises_not_found_error():
    """``load_strategy`` returning ``None`` → ``NotFoundError``."""
    services = _make_services(load_return=None)
    with _runner_patch() as ctor, pytest.raises(NotFoundError) as exc_info:
        await run_backtest(services, PRINCIPAL, BASE_ARGS)

    assert str(exc_info.value) == "Strategy not found: momentum"
    services.plugin_registry.load_strategy.assert_called_once_with("momentum")
    # The runner must not be constructed when the strategy is missing.
    ctor.assert_not_called()


# ── 3. No market-data provider configured ───────────────────────────────── #
async def test_run_backtest_no_provider_raises_engine_error():
    """A ``None`` provider from the factory → ``EngineError``."""
    services = _make_services(provider_factory=lambda: None)
    with _runner_patch() as ctor, pytest.raises(EngineError) as exc_info:
        await run_backtest(services, PRINCIPAL, BASE_ARGS)

    assert "No market-data provider configured" in str(exc_info.value)
    ctor.assert_not_called()


# ── 4. Runner execution failure ─────────────────────────────────────────── #
async def test_run_backtest_runner_failure_wrapped_as_engine_error():
    """An exception from ``runner.run()`` is normalised to ``EngineError``."""
    services = _make_services()
    with (
        _runner_patch(raises=RuntimeError("boom in data feed")) as ctor,
        pytest.raises(EngineError) as exc_info,
    ):
        await run_backtest(services, PRINCIPAL, BASE_ARGS)

    msg = str(exc_info.value)
    assert "Backtest execution failed" in msg
    assert "RuntimeError" in msg
    # The original exception is preserved on __cause__ for diagnostics.
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    ctor.return_value.run.assert_awaited_once()


@pytest.mark.parametrize(
    "exc",
    [ValueError("bad range"), KeyError("missing"), RuntimeError("oops")],
    ids=["value-error", "key-error", "runtime-error"],
)
async def test_run_backtest_runner_failure_chains_original(exc):
    """Different runner exception types are all wrapped, with ``__cause__`` set."""
    services = _make_services()
    with _runner_patch(raises=exc), pytest.raises(EngineError) as exc_info:
        await run_backtest(services, PRINCIPAL, BASE_ARGS)
    assert exc_info.value.__cause__ is exc


# ── 5. Happy path: summary shape & metrics formatting ───────────────────── #
async def test_run_backtest_success_returns_metrics_summary():
    """The happy path returns a compact, JSON-serialisable summary."""
    result = _make_result(
        final_capital=112_345.6789,
        total_return_pct=12.35,
        trades=[{"side": "buy"}, {"side": "sell"}],
        equity_curve=[{"t": 1}, {"t": 2}, {"t": 3}, {"t": 4}],
        metrics={
            "sharpe": 1.5,
            "max_drawdown": -0.12,
            # An evaluation dict must be hoisted to the top-level key, NOT
            # left inside ``metrics``.
            "evaluation": {"grade": "A", "notes": "solid"},
            # List-valued metrics are stripped from the ``metrics`` map.
            "equity_series": [1.0, 2.0, 3.0],
        },
    )
    services = _make_services()

    with _runner_patch(result=result) as ctor:
        summary = await run_backtest(services, PRINCIPAL, BASE_ARGS)

    # ── Echoed request fields ──
    assert summary["strategy_name"] == "momentum"
    assert summary["symbol"] == "AAPL"
    assert summary["start_date"] == "2023-01-01"
    assert summary["end_date"] == "2023-06-30"
    assert summary["initial_capital"] == 100_000.0

    # ── Result fields ──
    assert summary["final_capital"] == pytest.approx(112_345.6789)
    assert summary["total_return_pct"] == pytest.approx(12.35)
    assert summary["total_trades"] == 2
    assert summary["equity_points"] == 4

    # ── Metrics filtering ──
    assert summary["metrics"] == {"sharpe": 1.5, "max_drawdown": -0.12}
    assert "evaluation" not in summary["metrics"]
    assert "equity_series" not in summary["metrics"]
    assert summary["evaluation"] == {"grade": "A", "notes": "solid"}

    # ── Result-size sentinel ──
    assert summary["equity_curve_truncated"] == mcp_settings.result_token_budget

    # ── Runner wiring ──
    ctor.assert_called_once()
    ctor.return_value.run.assert_awaited_once_with()


async def test_run_backtest_success_omits_evaluation_when_absent():
    """When metrics has no ``evaluation`` key, ``summary['evaluation']`` is None."""
    result = _make_result(metrics={"sharpe": 0.8})
    services = _make_services()
    with _runner_patch(result=result):
        summary = await run_backtest(services, PRINCIPAL, BASE_ARGS)
    assert summary["evaluation"] is None
    assert summary["metrics"] == {"sharpe": 0.8}


async def test_run_backtest_success_sanitises_nan_and_inf_metrics():
    """NaN/inf metric values are normalised to ``None`` for valid JSON.

    ``to_jsonable`` walks the whole summary, so non-finite floats in
    ``final_capital`` / ``total_return_pct`` / metric values all become None.
    """
    result = _make_result(
        final_capital=float("inf"),
        total_return_pct=float("nan"),
        metrics={
            "sharpe": float("nan"),
            "max_drawdown": float("-inf"),
            "sortino": float("inf"),
            "win_rate": 0.5,  # finite — must be preserved
        },
    )
    services = _make_services()
    with _runner_patch(result=result):
        summary = await run_backtest(services, PRINCIPAL, BASE_ARGS)

    assert summary["final_capital"] is None
    assert summary["total_return_pct"] is None
    assert summary["metrics"]["sharpe"] is None
    assert summary["metrics"]["max_drawdown"] is None
    assert summary["metrics"]["sortino"] is None
    assert summary["metrics"]["win_rate"] == 0.5


async def test_run_backtest_success_counts_empty_trades_and_equity():
    """A result with no trades / equity still yields numeric counts."""
    result = _make_result(trades=[], equity_curve=[], metrics={})
    services = _make_services()
    with _runner_patch(result=result):
        summary = await run_backtest(services, PRINCIPAL, BASE_ARGS)
    assert summary["total_trades"] == 0
    assert summary["equity_points"] == 0
    assert summary["metrics"] == {}


# ── 6. BacktestConfig wiring ────────────────────────────────────────────── #
async def test_run_backtest_constructs_config_with_request_fields():
    """The ``BacktestConfig`` passed to the runner mirrors the request."""
    result = _make_result()
    services = _make_services()
    args = {
        "strategy_name": "mean_reversion",
        "symbol": "MSFT",
        "start_date": "2022-01-01",
        "end_date": "2022-12-31",
        "initial_capital": 250_000,
    }

    with _runner_patch(result=result) as ctor:
        await run_backtest(services, PRINCIPAL, args)

    config: BacktestConfig = ctor.call_args.kwargs["config"]
    assert isinstance(config, BacktestConfig)
    assert config.strategy_name == "mean_reversion"
    assert config.symbol == "MSFT"
    assert config.start_date == "2022-01-01"
    assert config.end_date == "2022-12-31"
    # ``initial_capital`` is coerced to float regardless of input type.
    assert config.initial_capital == 250_000.0
    assert isinstance(config.initial_capital, float)

    # The strategy object and provider flow straight through.
    assert ctor.call_args.kwargs["strategy"] == "STRATEGY"
    assert ctor.call_args.kwargs["provider"] is not None


async def test_run_backtest_uses_default_capital_when_omitted():
    """Omitting ``initial_capital`` defaults to 100,000 (as float)."""
    services = _make_services()
    with _runner_patch(result=_make_result()) as ctor:
        await run_backtest(services, PRINCIPAL, BASE_ARGS)
    config: BacktestConfig = ctor.call_args.kwargs["config"]
    assert config.initial_capital == 100_000.0


async def test_run_backtest_coerces_string_capital():
    """A numeric-string capital is accepted (``float()`` coercion)."""
    services = _make_services()
    args = {**BASE_ARGS, "initial_capital": "50000"}
    with _runner_patch(result=_make_result()) as ctor:
        await run_backtest(services, PRINCIPAL, args)
    config: BacktestConfig = ctor.call_args.kwargs["config"]
    assert config.initial_capital == 50_000.0


# ── 7. Progress reporting lifecycle ─────────────────────────────────────── #
async def test_run_backtest_progress_reports_start_and_complete_on_success():
    """On success the reporter sees a 0% start then a 100% complete."""
    progress = _FakeProgress()
    services = _make_services()
    with _runner_patch(result=_make_result()):
        await run_backtest(services, PRINCIPAL, BASE_ARGS, progress=progress)

    assert len(progress.calls) == 2
    start_progress, start_total, start_message = progress.calls[0]
    complete_progress, complete_total, complete_message = progress.calls[1]

    assert start_progress == 0
    assert start_total == 100
    assert "momentum" in start_message and "AAPL" in start_message

    assert complete_progress == 100
    assert complete_total == 100
    assert complete_message == "Backtest complete"


async def test_run_backtest_progress_complete_not_reported_on_failure():
    """A runner failure reports start but never the 100% completion."""
    progress = _FakeProgress()
    services = _make_services()
    with _runner_patch(raises=RuntimeError("kaboom")), pytest.raises(EngineError):
        await run_backtest(services, PRINCIPAL, BASE_ARGS, progress=progress)

    assert len(progress.calls) == 1
    assert progress.calls[0][0] == 0  # only the start was reported


async def test_run_backtest_progress_none_is_safe():
    """``progress=None`` (the default) must not raise."""
    services = _make_services()
    with _runner_patch(result=_make_result()):
        summary = await run_backtest(services, PRINCIPAL, BASE_ARGS, progress=None)
    # Still produces a valid summary.
    assert summary["strategy_name"] == "momentum"


# ── 8. dispatch_tool integration ────────────────────────────────────────── #
async def test_dispatch_tool_routes_run_backtest():
    """``dispatch_tool('run_backtest', ...)`` delegates to the adapter."""
    result = _make_result(metrics={"sharpe": 2.0})
    services = _make_services()

    with _runner_patch(result=result) as ctor:
        out = await dispatch_tool("run_backtest", BASE_ARGS, services, PRINCIPAL)

    ctor.return_value.run.assert_awaited_once()
    assert out["strategy_name"] == "momentum"
    assert out["metrics"] == {"sharpe": 2.0}
    assert out["equity_curve_truncated"] == mcp_settings.result_token_budget


async def test_dispatch_tool_run_backtest_strategy_not_found_propagates():
    """A not-found error propagates through ``dispatch_tool`` unchanged."""
    services = _make_services(load_return=None)
    with _runner_patch() as ctor, pytest.raises(NotFoundError):
        await dispatch_tool("run_backtest", BASE_ARGS, services, PRINCIPAL)
    ctor.assert_not_called()


async def test_dispatch_tool_run_backtest_missing_required_arg():
    """``dispatch_tool`` validates required args before dispatch."""
    services = _make_services()
    incomplete = {"strategy_name": "momentum", "symbol": "AAPL"}  # no dates
    with _runner_patch() as ctor, pytest.raises(ValidationError) as exc_info:
        await dispatch_tool("run_backtest", incomplete, services, PRINCIPAL)
    assert "start_date" in str(exc_info.value)
    ctor.assert_not_called()


async def test_dispatch_tool_unknown_tool_rejected():
    """An unknown tool name is rejected with ``ValidationError``."""
    services = _make_services()
    with pytest.raises(ValidationError) as exc_info:
        await dispatch_tool("does_not_exist", {}, services, PRINCIPAL)
    assert "Unknown tool" in str(exc_info.value)


async def test_dispatch_tool_run_backtest_mcp_errors_not_rewrapped():
    """``MCPError`` subclasses pass straight through (not double-wrapped)."""
    services = _make_services(load_return=None)
    with _runner_patch(), pytest.raises(MCPError) as exc_info:
        await dispatch_tool("run_backtest", BASE_ARGS, services, PRINCIPAL)
    # Stays a NotFoundError, not re-wrapped into a generic EngineError.
    assert isinstance(exc_info.value, NotFoundError)
