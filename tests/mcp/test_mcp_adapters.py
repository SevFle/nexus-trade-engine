"""Unit tests for the most-recently-changed MCP adapter code.

Targets the adapter layer touched in commit ``cd5ec79`` ("fix(mcp): resolve
adapter issues causing CI quality gate failure"), namely:

* :mod:`engine.mcp.adapters.market_data_adapter` — ``get_market_data``,
  ``get_cost_model``, ``get_performance_metrics`` and the ``_safe_float`` /
  ``_MIN_EQUITY_POINTS`` helpers introduced in that change.
* :mod:`engine.mcp.adapters` — ``EngineServices.for_testing`` injection,
  ``to_jsonable`` JSON normalisation, and ``PortfolioStore``.

The adapters are designed as pure functions ``(services, principal, arguments)
-> dict``. That purity is exactly the property the task calls a *dry-run
mode*: running the workflow logic against a sample set of "issues" must not
mutate any input or shared state. The ``TestDryRunNoMutations`` class below
asserts that contract end-to-end.
"""
from __future__ import annotations

import copy
import math
from typing import Any, ClassVar

import pandas as pd
import pytest

from engine.mcp.adapters import (
    EngineServices,
    PortfolioStore,
    to_jsonable,
)
from engine.mcp.adapters.market_data_adapter import (
    _MIN_EQUITY_POINTS,
    _safe_float,
    get_cost_model,
    get_market_data,
    get_performance_metrics,
)
from engine.mcp.errors import EngineError, ValidationError

from .conftest import FakeCostModel, FakeMarketDataProvider, make_services

# ---------------------------------------------------------------------------
# _safe_float helper
# ---------------------------------------------------------------------------


class TestSafeFloat:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1, 1.0),
            (1.5, 1.5),
            ("3.14", 3.14),
            (True, 1.0),
            (-7, -7.0),
        ],
    )
    def test_finite_values(self, value: Any, expected: float) -> None:
        assert _safe_float(value) == expected

    @pytest.mark.parametrize("value", [None])
    def test_none_rejected(self, value: Any) -> None:
        with pytest.raises(ValueError):
            _safe_float(value)

    def test_nan_rejected(self) -> None:
        with pytest.raises(ValueError):
            _safe_float(float("nan"))

    def test_inf_rejected(self) -> None:
        with pytest.raises(ValueError):
            _safe_float(float("inf"))
        with pytest.raises(ValueError):
            _safe_float(float("-inf"))

    def test_min_equity_points_constant(self) -> None:
        # The cd5ec79 change factored the magic ``2`` into a named constant.
        assert _MIN_EQUITY_POINTS == 2


# ---------------------------------------------------------------------------
# get_market_data
# ---------------------------------------------------------------------------


class TestGetMarketData:
    async def test_missing_symbol_raises_validation_error(
        self, principal: Any, make_df: pd.DataFrame
    ) -> None:
        services = make_services(provider=FakeMarketDataProvider(df=make_df))
        with pytest.raises(ValidationError):
            await get_market_data(services, principal, {})

    async def test_no_provider_configured_raises_engine_error(
        self, principal: Any
    ) -> None:
        # EngineServices with a provider factory that returns None.
        services = EngineServices.for_testing()
        services.market_data_provider_factory = lambda: None  # type: ignore[assignment]
        with pytest.raises(EngineError):
            await get_market_data(services, principal, {"symbol": "AAPL"})

    async def test_empty_dataframe_returns_empty_bars(
        self, principal: Any
    ) -> None:
        provider = FakeMarketDataProvider(df=pd.DataFrame())
        services = make_services(provider=provider)
        result = await get_market_data(services, principal, {"symbol": "AAPL"})

        assert result["symbol"] == "AAPL"
        assert result["bars"] == []
        assert provider.calls == [("AAPL", "1y", "1d")]

    async def test_none_dataframe_returns_empty_bars(
        self, principal: Any
    ) -> None:
        provider = FakeMarketDataProvider(df=None)
        services = make_services(provider=provider)
        result = await get_market_data(services, principal, {"symbol": "AAPL"})
        assert result["bars"] == []

    async def test_valid_bars_normalised(
        self, principal: Any, make_df: pd.DataFrame
    ) -> None:
        provider = FakeMarketDataProvider(df=make_df)
        services = make_services(provider=provider)
        result = await get_market_data(
            services,
            principal,
            {"symbol": "AAPL", "interval": "1h", "period": "1mo"},
        )

        assert result["symbol"] == "AAPL"
        assert result["interval"] == "1h"
        assert result["period"] == "1mo"
        assert len(result["bars"]) == 3
        first = result["bars"][0]
        assert first["open"] == 100.0
        assert first["close"] == 105.0
        assert first["volume"] == 1_000.0
        # Timestamps serialise via isoformat().
        assert first["timestamp"].startswith("2026-01-01")

    async def test_provider_exception_maps_to_engine_error(
        self, principal: Any
    ) -> None:
        provider = FakeMarketDataProvider(exc=RuntimeError("boom"))
        services = make_services(provider=provider)
        with pytest.raises(EngineError) as excinfo:
            await get_market_data(services, principal, {"symbol": "AAPL"})
        # Internal detail is sanitised — only the exception class name leaks.
        assert "RuntimeError" in str(excinfo.value)
        assert "boom" not in str(excinfo.value)

    async def test_nan_rows_are_dropped(self, principal: Any) -> None:
        idx = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"])
        df = pd.DataFrame(
            {
                "open": [100.0, float("nan"), 102.0],
                "high": [110.0, 111.0, 112.0],
                "low": [99.0, 100.0, 101.0],
                "close": [105.0, 106.0, 107.0],
                "volume": [1_000.0, 1_500.0, 2_000.0],
            },
            index=idx,
        )
        provider = FakeMarketDataProvider(df=df)
        services = make_services(provider=provider)
        result = await get_market_data(services, principal, {"symbol": "AAPL"})

        # The middle row (NaN open) is skipped, leaving 2 valid bars.
        assert len(result["bars"]) == 2
        assert all(math.isfinite(b["open"]) for b in result["bars"])

    async def test_default_interval_and_period_used(
        self, principal: Any, make_df: pd.DataFrame
    ) -> None:
        provider = FakeMarketDataProvider(df=make_df)
        services = make_services(provider=provider)
        await get_market_data(services, principal, {"symbol": "AAPL"})
        assert provider.calls == [("AAPL", "1y", "1d")]


# ---------------------------------------------------------------------------
# get_cost_model
# ---------------------------------------------------------------------------


class TestGetCostModel:
    async def test_missing_arguments_raise_validation_error(
        self, principal: Any
    ) -> None:
        services = make_services()
        for bad in (
            {},
            {"symbol": "AAPL"},
            {"symbol": "AAPL", "quantity": 10},
            {"quantity": 10, "price": 100.0},
        ):
            with pytest.raises(ValidationError):
                await get_cost_model(services, principal, bad)

    async def test_quantity_below_one_rejected(self, principal: Any) -> None:
        services = make_services()
        with pytest.raises(ValidationError):
            await get_cost_model(
                services,
                principal,
                {"symbol": "AAPL", "quantity": 0, "price": 100.0},
            )

    async def test_non_positive_price_rejected(self, principal: Any) -> None:
        services = make_services()
        for price in (0.0, -5.0):
            with pytest.raises(ValidationError):
                await get_cost_model(
                    services,
                    principal,
                    {"symbol": "AAPL", "quantity": 10, "price": price},
                )

    async def test_non_numeric_arguments_rejected(self, principal: Any) -> None:
        services = make_services()
        with pytest.raises(ValidationError):
            await get_cost_model(
                services,
                principal,
                {"symbol": "AAPL", "quantity": "lots", "price": 100.0},
            )

    async def test_valid_call_returns_expected_shape(
        self, principal: Any, cost_model: FakeCostModel
    ) -> None:
        services = make_services(cost_model=cost_model)
        result = await get_cost_model(
            services,
            principal,
            {
                "symbol": "AAPL",
                "quantity": 100,
                "price": 150.0,
                "side": "sell",
                "avg_volume": 1_000_000,
            },
        )

        assert result["symbol"] == "AAPL"
        assert result["quantity"] == 100
        assert result["price"] == 150.0
        assert result["side"] == "sell"
        assert result["notional"] == 15_000.0
        assert result["cost_pct_of_notional"] == pytest.approx(1.0)  # 0.01 * 100
        assert result["total_cost"] == 5.0
        assert result["total_cost_excluding_tax"] == 5.0
        assert "breakdown" in result and isinstance(result["breakdown"], dict)

        # The cost model received exactly the parsed/coerced values.
        assert cost_model.estimate_total_calls == [
            {
                "symbol": "AAPL",
                "quantity": 100,
                "price": 150.0,
                "side": "sell",
                "avg_volume": 1_000_000,
            }
        ]

    async def test_defaults_side_and_avg_volume(self, principal: Any) -> None:
        services = make_services()
        result = await get_cost_model(
            services,
            principal,
            {"symbol": "AAPL", "quantity": 10, "price": 100.0},
        )
        assert result["side"] == "buy"
        assert services.cost_model.estimate_total_calls[-1]["avg_volume"] == 0


# ---------------------------------------------------------------------------
# get_performance_metrics
# ---------------------------------------------------------------------------


class _FakeReport:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, Any]:
        return dict(self._payload)


class _FakePerformanceMetrics:
    """Replaces ``engine.core.metrics.PerformanceMetrics`` for hermetic tests."""

    instances: ClassVar[list[_FakePerformanceMetrics]] = []

    def __init__(self, equity_curve, trade_log, initial_cash, **_kwargs):
        self.equity_curve = equity_curve
        self.trade_log = trade_log
        self.initial_cash = initial_cash
        self._payload = {
            "total_return_pct": 10.0,
            "sharpe_ratio": float("nan"),  # exercises to_jsonable NaN→None
            "volatility_annual_pct": 12.0,
            "data_points": len(equity_curve),
        }
        type(self).instances.append(self)

    def calculate(self) -> _FakeReport:
        return _FakeReport(self._payload)


class TestGetPerformanceMetrics:
    @pytest.mark.parametrize(
        "curve",
        [[], [{"total_value": 1.0}], "not-a-list", None],
    )
    async def test_invalid_equity_curve_rejected(
        self, principal: Any, curve: Any
    ) -> None:
        services = make_services()
        with pytest.raises(ValidationError):
            await get_performance_metrics(
                services, principal, {"equity_curve": curve}
            )

    async def test_missing_key_rejected(self, principal: Any) -> None:
        services = make_services()
        with pytest.raises(ValidationError):
            await get_performance_metrics(services, principal, {})

    async def test_valid_curve_returns_report(
        self, principal: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _FakePerformanceMetrics.instances.clear()
        monkeypatch.setattr(
            "engine.core.metrics.PerformanceMetrics", _FakePerformanceMetrics
        )
        services = make_services()
        curve = [{"total_value": 100.0}, {"total_value": 110.0}]
        result = await get_performance_metrics(
            services, principal, {"equity_curve": curve, "initial_capital": 50_000.0}
        )

        assert result["initial_capital"] == 50_000.0
        assert result["data_points"] == 2
        assert result["metrics"]["total_return_pct"] == 10.0
        # NaN normalised to None by to_jsonable (valid-JSON guarantee).
        assert result["metrics"]["sharpe_ratio"] is None

        # _services / _principal are unused — the real metrics engine is driven
        # solely by the arguments, matching the cd5ec79 signature change.
        fake = _FakePerformanceMetrics.instances[-1]
        assert fake.initial_cash == 50_000.0
        assert fake.trade_log == []

    async def test_default_initial_capital(
        self, principal: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _FakePerformanceMetrics.instances.clear()
        monkeypatch.setattr(
            "engine.core.metrics.PerformanceMetrics", _FakePerformanceMetrics
        )
        services = make_services()
        await get_performance_metrics(
            services, principal, {"equity_curve": [1.0, 2.0]}
        )
        assert _FakePerformanceMetrics.instances[-1].initial_cash == 100_000.0

    async def test_metrics_exception_maps_to_engine_error(
        self, principal: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Boom:
            def __init__(self, *a, **k):
                pass

            def calculate(self):
                raise ZeroDivisionError("nope")

        monkeypatch.setattr("engine.core.metrics.PerformanceMetrics", _Boom)
        services = make_services()
        with pytest.raises(EngineError) as excinfo:
            await get_performance_metrics(
                services, principal, {"equity_curve": [1.0, 2.0]}
            )
        assert "ZeroDivisionError" in str(excinfo.value)
        assert "nope" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Dry-run / no-mutation contract (the task's requested focus)
# ---------------------------------------------------------------------------


def _deep_snapshot(obj: Any) -> Any:
    """An independent deep copy used to detect any mutation."""
    return copy.deepcopy(obj)


class TestDryRunNoMutations:
    """Run the adapter workflow logic over a sample set of "issues" and assert
    that *nothing* is mutated — the dry-run / read-only contract.

    Each "issue" is a representative argument payload. We snapshot inputs
    (arguments + injected fake state) before the call and compare after.
    """

    @staticmethod
    def _sample_issues() -> list[dict[str, Any]]:
        return [
            # market-data reads
            {"adapter": "market_data", "arguments": {"symbol": "AAPL"}},
            {
                "adapter": "market_data",
                "arguments": {"symbol": "MSFT", "interval": "1h", "period": "1mo"},
            },
            # cost-model reads
            {
                "adapter": "cost_model",
                "arguments": {
                    "symbol": "AAPL",
                    "quantity": 100,
                    "price": 150.0,
                    "side": "buy",
                    "avg_volume": 1_000_000,
                },
            },
            {
                "adapter": "cost_model",
                "arguments": {"symbol": "GOOG", "quantity": 5, "price": 2_800.0},
            },
            # performance-metrics reads
            {
                "adapter": "performance",
                "arguments": {
                    "equity_curve": [
                        {"total_value": 100_000.0},
                        {"total_value": 110_000.0},
                        {"total_value": 105_000.0},
                    ],
                    "initial_capital": 100_000.0,
                },
            },
        ]

    async def test_no_input_or_state_mutated_across_sample_issues(
        self,
        principal: Any,
        make_df: pd.DataFrame,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "engine.core.metrics.PerformanceMetrics", _FakePerformanceMetrics
        )
        provider = FakeMarketDataProvider(df=make_df)
        cost_model = FakeCostModel()
        services = make_services(provider=provider, cost_model=cost_model)

        for issue in self._sample_issues():
            args = issue["arguments"]
            adapter = issue["adapter"]

            args_before = _deep_snapshot(args)
            provider_calls_before = list(provider.calls)
            cost_total_before = _deep_snapshot(cost_model.estimate_total_calls)
            cost_pct_before = _deep_snapshot(cost_model.estimate_pct_calls)

            if adapter == "market_data":
                await get_market_data(services, principal, args)
            elif adapter == "cost_model":
                await get_cost_model(services, principal, args)
            elif adapter == "performance":
                await get_performance_metrics(services, principal, args)
            else:  # pragma: no cover - defensive
                raise AssertionError(f"unknown adapter {adapter!r}")

            # The caller's argument dict is never mutated in place.
            assert args == args_before, f"arguments mutated for {adapter!r}"

            # Prior recorded calls are preserved (append-only, no rewriting).
            assert provider.calls[: len(provider_calls_before)] == provider_calls_before
            assert (
                cost_model.estimate_total_calls[: len(cost_total_before)]
                == cost_total_before
            )
            assert (
                cost_model.estimate_pct_calls[: len(cost_pct_before)]
                == cost_pct_before
            )

    async def test_market_data_result_does_not_alias_input_dataframe(
        self, principal: Any, make_df: pd.DataFrame
    ) -> None:
        provider = FakeMarketDataProvider(df=make_df)
        services = make_services(provider=provider)

        df_before = _deep_snapshot(make_df)
        result = await get_market_data(services, principal, {"symbol": "AAPL"})

        # Returned bars are plain JSON values, independent of the source frame.
        assert isinstance(result["bars"], list)
        assert make_df.equals(df_before)
        for bar in result["bars"]:
            bar["open"] = 999.0  # mutating the output must not touch the df
        assert make_df.equals(df_before)

    async def test_repeated_calls_are_idempotent(
        self, principal: Any, make_df: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "engine.core.metrics.PerformanceMetrics", _FakePerformanceMetrics
        )
        provider = FakeMarketDataProvider(df=make_df)
        services = make_services(provider=provider, cost_model=FakeCostModel())

        args = {"symbol": "AAPL", "quantity": 10, "price": 100.0}
        snapshot = _deep_snapshot(args)
        first = await get_cost_model(services, principal, dict(args))
        second = await get_cost_model(services, principal, dict(args))
        assert first == second
        assert args == snapshot


# ---------------------------------------------------------------------------
# adapters package: EngineServices injection, to_jsonable, PortfolioStore
# (adapters/__init__.py was also part of the cd5ec79 change)
# ---------------------------------------------------------------------------


class TestEngineServicesInjection:
    def test_for_testing_pins_single_provider_instance(self) -> None:
        provider = FakeMarketDataProvider()
        services = EngineServices.for_testing(market_data_provider=provider)
        # The factory returns the same captured instance every call.
        assert services.market_data_provider_factory() is provider
        assert services.market_data_provider_factory() is provider

    def test_for_testing_defaults_when_no_provider_given(self) -> None:
        services = EngineServices.for_testing()
        # Default factory builds a fresh provider on each invocation.
        a = services.market_data_provider_factory()
        b = services.market_data_provider_factory()
        assert a is not b

    def test_cost_model_injectable(self) -> None:
        cost_model = FakeCostModel(total=42.0)
        services = EngineServices.for_testing(cost_model=cost_model)
        assert services.cost_model is cost_model


class TestToJsonable:
    def test_nan_and_inf_become_none(self) -> None:
        out = to_jsonable([float("nan"), float("inf"), float("-inf"), 1.0])
        assert out == [None, None, None, 1.0]

    def test_nested_structure_normalised(self) -> None:
        import datetime as dt
        from decimal import Decimal

        payload = {
            "price": Decimal("10.5"),
            "when": dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC),
            "nested": {"x": float("nan")},
            "items": (1, 2, 3),
        }
        out = to_jsonable(payload)
        assert out["price"] == 10.5
        assert out["when"].startswith("2026-01-01")
        assert out["nested"] == {"x": None}
        assert out["items"] == [1, 2, 3]

    def test_passes_through_strings(self) -> None:
        assert to_jsonable("hello") == "hello"
        assert to_jsonable(None) is None


class TestPortfolioStore:
    def test_default_portfolio_seeded(self) -> None:
        store = PortfolioStore(default_capital=250_000.0)
        assert "default" in store.list_ids()
        assert store.get("default").initial_cash == 250_000.0

    def test_get_unknown_id_seeds_on_demand(self) -> None:
        store = PortfolioStore()
        assert "alpha" not in store.list_ids()
        store.get("alpha")
        assert "alpha" in store.list_ids()

    def test_seed_creates_and_overwrites(self) -> None:
        store = PortfolioStore()
        store.seed("default", 999.0)
        assert store.get("default").initial_cash == 999.0

    def test_list_ids_sorted(self) -> None:
        store = PortfolioStore()
        store.get("zeta")
        store.get("alpha")
        assert store.list_ids() == sorted(store.list_ids())
