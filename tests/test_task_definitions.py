"""Unit tests for ``engine.tasks.definitions``.

Covers the public API surface:
- :func:`with_retry` retry-count semantics (success path, persistent
  retryable failure, eventual success, non-retryable fast-fail, zero
  retries, jittered backoff delays).
- :func:`run_backtest` happy path (completed envelope) and error handling
  (unknown strategy + retried-infra failure → failed envelope).
- :func:`_execute_backtest` happy path and timeout retry behaviour.

All external dependencies (data provider, plugin registry, backtest
runner, the retry sleeper and the RNG used for jitter) are mocked so the
tests are deterministic and never touch Redis/Valkey or the network.
"""

from __future__ import annotations

import asyncio
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from engine.tasks import definitions


# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def fast_retry_sleep(monkeypatch):
    """Replace the retry backoff sleeper with a recording stub.

    ``with_retry`` looks the sleeper up as the module-level ``_retry_sleep``
    attribute at call time, so monkeypatching it makes every test
    instantaneous while still letting us assert on the computed delays.
    """
    delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(definitions, "_retry_sleep", _fake_sleep)
    return delays


@pytest.fixture
def fixed_jitter(monkeypatch):
    """Pin the jitter RNG so backoff delays are deterministic."""
    monkeypatch.setattr(definitions.random, "random", lambda: 1.0)


@pytest.fixture
def backtest_result():
    """A realistic :class:`BacktestResult` for the happy-path assertions."""
    from engine.core.backtest_runner import BacktestResult

    return BacktestResult(
        trades=[
            {"side": "buy", "qty": 10, "price": 100.0},
            {"side": "sell", "qty": 10, "price": 112.0},
        ],
        total_return_pct=12.34567,
        final_capital=112_345.6789,
        metrics={"sharpe": 1.5, "max_drawdown": -0.12},
    )


@pytest.fixture
def mock_engine(monkeypatch, backtest_result):
    """Patch the three lazy imports ``_execute_backtest`` performs.

    Returns a namespace exposing every mock so individual tests can re-wire
    ``runner.run`` to raise, succeed, or flap.
    """
    provider = MagicMock(name="provider")

    runner = MagicMock(name="BacktestRunner-instance")
    runner.run = AsyncMock(return_value=backtest_result)
    runner_cls = MagicMock(return_value=runner)

    strategy = MagicMock(name="strategy")
    registry_instance = MagicMock(name="PluginRegistry-instance")
    registry_instance.load_strategy.return_value = strategy
    registry_cls = MagicMock(return_value=registry_instance)

    monkeypatch.setattr("engine.data.feeds.get_data_provider", lambda name="yahoo": provider)
    monkeypatch.setattr("engine.plugins.registry.PluginRegistry", registry_cls)
    monkeypatch.setattr("engine.core.backtest_runner.BacktestRunner", runner_cls)

    return SimpleNamespace(
        provider=provider,
        runner=runner,
        runner_cls=runner_cls,
        strategy=strategy,
        registry_instance=registry_instance,
        registry_cls=registry_cls,
        result=backtest_result,
    )


# --------------------------------------------------------------------------- #
# with_retry
# --------------------------------------------------------------------------- #
class TestWithRetry:
    def test_returns_result_on_success_without_retry(self):
        @definitions.with_retry(max_retries=3)
        async def succeeds():
            return "ok"

        # ``run_backtest``-style: it is a plain awaitable wrapper.
        result = asyncio.run(succeeds())
        assert result == "ok"

    async def test_no_sleep_invoked_on_success(self, fast_retry_sleep):
        @definitions.with_retry(max_retries=3)
        async def succeeds():
            return 42

        assert await succeeds() == 42
        assert fast_retry_sleep == []  # nothing to back off from

    async def test_retries_persistently_failing_retryable_then_raises(
        self, fast_retry_sleep, fixed_jitter
    ):
        calls = 0

        @definitions.with_retry(max_retries=3, base_delay=0.1, max_delay=2.0)
        async def always_fails():
            nonlocal calls
            calls += 1
            raise ConnectionError("boom")

        with pytest.raises(definitions.TaskExecutionError) as exc_info:
            await always_fails()

        # 1 initial attempt + 3 retries == 4 total invocations.
        assert calls == 4
        # Three backoff sleeps (after attempts 1, 2, 3); the 4th attempt
        # breaks out before sleeping.
        assert fast_retry_sleep == [0.1, 0.2, 0.4]
        # The original exception is preserved on __cause__ for diagnostics.
        assert isinstance(exc_info.value.__cause__, ConnectionError)
        assert "always_fails" in str(exc_info.value)
        assert "4 attempts" in str(exc_info.value)

    async def test_eventually_succeeds_after_transient_failures(
        self, fast_retry_sleep, fixed_jitter
    ):
        calls = 0

        @definitions.with_retry(max_retries=3, base_delay=0.1, max_delay=2.0)
        async def flaky():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise TimeoutError("transient")
            return "recovered"

        assert await flaky() == "recovered"
        # Failed on attempts 1 and 2, succeeded on attempt 3 → 2 sleeps.
        assert calls == 3
        assert fast_retry_sleep == [0.1, 0.2]

    async def test_non_retryable_exception_propagates_immediately(self, fast_retry_sleep):
        calls = 0

        @definitions.with_retry(max_retries=5)
        async def bad_input():
            nonlocal calls
            calls += 1
            raise ValueError("unknown strategy")

        # ValueError is not in the default retryable tuple, so it must
        # surface unwrapped on the very first attempt.
        with pytest.raises(ValueError, match="unknown strategy"):
            await bad_input()

        assert calls == 1
        assert fast_retry_sleep == []  # never reached the retry branch

    async def test_max_retries_zero_calls_once(self, fast_retry_sleep):
        calls = 0

        @definitions.with_retry(max_retries=0)
        async def always_fails():
            nonlocal calls
            calls += 1
            raise TimeoutError

        with pytest.raises(definitions.TaskExecutionError) as exc_info:
            await always_fails()

        assert calls == 1
        assert fast_retry_sleep == []
        assert isinstance(exc_info.value.__cause__, TimeoutError)

    async def test_custom_retryable_tuple_is_respected(self, fast_retry_sleep):
        calls = 0

        @definitions.with_retry(max_retries=2, retryable=(KeyError,))
        async def maybe():
            nonlocal calls
            calls += 1
            raise KeyError("only-this-is-retried")

        with pytest.raises(definitions.TaskExecutionError) as exc_info:
            await maybe()

        assert calls == 3  # 1 + 2 retries
        assert isinstance(exc_info.value.__cause__, KeyError)

    async def test_backoff_capped_at_max_delay(self, fast_retry_sleep, fixed_jitter):
        calls = 0

        # base_delay grows as 2**attempt but max_delay clamps every sleep.
        @definitions.with_retry(max_retries=4, base_delay=10.0, max_delay=1.0)
        async def always_fails():
            nonlocal calls
            calls += 1
            raise ConnectionError

        with pytest.raises(definitions.TaskExecutionError):
            await always_fails()

        # 4 retries → 4 sleeps, every one clamped to max_delay (jitter=1.0).
        assert fast_retry_sleep == [1.0, 1.0, 1.0, 1.0]


# --------------------------------------------------------------------------- #
# _execute_backtest
# --------------------------------------------------------------------------- #
class TestExecuteBacktest:
    async def test_happy_path_returns_runner_result(self, mock_engine, fast_retry_sleep):
        result = await definitions._execute_backtest(
            strategy_name="momentum",
            symbol="AAPL",
            start_date="2023-01-01",
            end_date="2023-06-01",
            initial_capital=50_000.0,
        )

        # Delegates straight to BacktestRunner.run().
        assert result is mock_engine.result
        mock_engine.runner.run.assert_awaited_once_with()

        # Strategy resolution + provider + runner wiring happened.
        mock_engine.registry_instance.load_strategy.assert_called_once_with("momentum")
        assert mock_engine.runner_cls.call_count == 1
        config = mock_engine.runner_cls.call_args.kwargs["config"]
        assert config.strategy_name == "momentum"
        assert config.symbol == "AAPL"
        assert config.initial_capital == 50_000.0
        assert mock_engine.runner_cls.call_args.kwargs["strategy"] is mock_engine.strategy
        assert mock_engine.runner_cls.call_args.kwargs["provider"] is mock_engine.provider
        # Happy path: no retries needed.
        assert fast_retry_sleep == []

    async def test_unknown_strategy_raises_value_error_immediately(
        self, mock_engine, fast_retry_sleep
    ):
        # ``load_strategy`` returning None → ValueError, which is NOT
        # retryable, so it must surface on attempt #1.
        mock_engine.registry_instance.load_strategy.return_value = None
        run_calls = mock_engine.runner.run

        with pytest.raises(ValueError, match="Strategy not found: ghost"):
            await definitions._execute_backtest(
                strategy_name="ghost",
                symbol="AAPL",
                start_date="2023-01-01",
                end_date="2023-06-01",
                initial_capital=100_000.0,
            )

        # The runner was never even constructed.
        run_calls.assert_not_awaited()
        mock_engine.runner_cls.assert_not_called()
        assert fast_retry_sleep == []

    async def test_timeout_retried_then_raises_task_error(
        self, mock_engine, fast_retry_sleep, fixed_jitter
    ):
        # _execute_backtest is decorated with max_retries=3.
        mock_engine.runner.run = AsyncMock(side_effect=TimeoutError)

        with pytest.raises(definitions.TaskExecutionError) as exc_info:
            await definitions._execute_backtest(
                strategy_name="momentum",
                symbol="AAPL",
                start_date="2023-01-01",
                end_date="2023-06-01",
                initial_capital=100_000.0,
            )

        # 1 initial + 3 retries == 4 runner.run() invocations.
        assert mock_engine.runner.run.await_count == 4
        assert fast_retry_sleep == [0.1, 0.2, 0.4]
        assert isinstance(exc_info.value.__cause__, TimeoutError)

    async def test_timeout_retried_then_recovers(
        self, mock_engine, fast_retry_sleep, fixed_jitter
    ):
        mock_engine.runner.run = AsyncMock(
            side_effect=[
                TimeoutError,
                TimeoutError,
                mock_engine.result,
            ]
        )

        result = await definitions._execute_backtest(
            strategy_name="momentum",
            symbol="AAPL",
            start_date="2023-01-01",
            end_date="2023-06-01",
            initial_capital=100_000.0,
        )

        assert result is mock_engine.result
        assert mock_engine.runner.run.await_count == 3
        assert fast_retry_sleep == [0.1, 0.2]


# --------------------------------------------------------------------------- #
# run_backtest (public task)
# --------------------------------------------------------------------------- #
class TestRunBacktest:
    async def test_success_returns_completed_envelope(self, mock_engine, fast_retry_sleep):
        payload = await definitions.run_backtest(
            "momentum", "AAPL", "2023-01-01", "2023-06-01", initial_capital=25_000.0
        )

        assert payload["status"] == "completed"
        assert payload["strategy_name"] == "momentum"
        assert payload["symbol"] == "AAPL"
        assert payload["start_date"] == "2023-01-01"
        assert payload["end_date"] == "2023-06-01"
        # Derived from the mocked BacktestResult.
        assert payload["total_trades"] == 2
        assert payload["total_return_pct"] == round(12.34567, 4)
        assert payload["final_capital"] == round(112_345.6789, 2)
        assert payload["metrics"] == {"sharpe": 1.5, "max_drawdown": -0.12}
        # correlation_id is always bound by the task entrypoint.
        assert payload["correlation_id"] is not None
        assert isinstance(payload["correlation_id"], str)

    async def test_success_payload_is_json_serialisable(self, mock_engine, fast_retry_sleep):
        # The Redis result backend serialises the dict, so it must survive
        # a json round-trip with no datetime/Decimal/NaN leakage.
        import json

        payload = await definitions.run_backtest("momentum", "AAPL", "2023-01-01", "2023-06-01")

        assert payload["status"] == "completed"
        json.loads(json.dumps(payload))  # raises if not serialisable

    async def test_unknown_strategy_returns_failed_envelope(self, mock_engine, fast_retry_sleep):
        mock_engine.registry_instance.load_strategy.return_value = None

        payload = await definitions.run_backtest("ghost", "AAPL", "2023-01-01", "2023-06-01")

        assert payload["status"] == "failed"
        assert payload["strategy_name"] == "ghost"
        assert payload["symbol"] == "AAPL"
        assert "Strategy not found: ghost" in payload["error"]
        assert payload["error_type"] == "ValueError"
        assert payload["correlation_id"] is not None
        # Non-retryable: runner never touched, no backoff sleeps.
        mock_engine.runner.run.assert_not_awaited()
        assert fast_retry_sleep == []

    async def test_retried_infra_failure_returns_failed_envelope(
        self, mock_engine, fast_retry_sleep, fixed_jitter
    ):
        # _execute_backtest retries 3x then raises TaskExecutionError, which
        # the public task must translate into a failed envelope.
        mock_engine.runner.run = AsyncMock(side_effect=ConnectionResetError())

        payload = await definitions.run_backtest("momentum", "AAPL", "2023-01-01", "2023-06-01")

        assert payload["status"] == "failed"
        assert payload["error_type"] == "TaskExecutionError"
        assert "_execute_backtest failed after 4 attempts" in payload["error"]
        assert payload["strategy_name"] == "momentum"
        assert payload["symbol"] == "AAPL"
        assert mock_engine.runner.run.await_count == 4
        assert fast_retry_sleep == [0.1, 0.2, 0.4]

    async def test_uses_default_initial_capital(self, mock_engine, fast_retry_sleep):
        await definitions.run_backtest("momentum", "AAPL", "2023-01-01", "2023-06-01")

        config = mock_engine.runner_cls.call_args.kwargs["config"]
        # Default documented in the public signature.
        assert config.initial_capital == 100_000.0
