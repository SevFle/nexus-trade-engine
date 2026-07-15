"""Focused tests for input validation + job submit/collect in ``definitions``.

These tests target the three changes made to :mod:`engine.tasks.definitions`:

1. **``_validate_backtest_inputs``** — the input-validation helper that
   rejects NaN/inf/negative ``initial_capital``, enforces
   ``start_date < end_date`` (strict, ISO ``YYYY-MM-DD``) and strips C0/C1
   control characters from ``strategy_name`` / ``symbol``.
2. **None task_id guard** in :func:`submit_backtest_job` — when the broker
   accepts the enqueue but returns no task id, the caller must receive a
   ``failed`` envelope instead of an un-pollable id.
3. The helper being wired into both public tasks so bad input fails fast.

The submit/collect paths previously had no coverage at all; this module
also exercises :func:`collect_backtest_result` (pending / completed /
error / backend failure) by stubbing :func:`_build_result_task`.
"""

from __future__ import annotations

import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from engine.tasks import definitions


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def fast_retry_sleep(monkeypatch):
    """Make ``with_retry`` backoff sleeps instantaneous and observable."""
    delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(definitions, "_retry_sleep", _fake_sleep)
    return delays


@pytest.fixture
def backtest_result():
    from engine.core.backtest_runner import BacktestResult

    return BacktestResult(
        trades=[{"side": "buy", "qty": 10, "price": 100.0}],
        total_return_pct=5.0,
        final_capital=105_000.0,
        metrics={"sharpe": 1.0},
    )


@pytest.fixture
def mock_engine(monkeypatch, backtest_result):
    """Patch the lazy imports ``_run_backtest_once`` performs.

    Mirrors the fixture in ``test_task_definitions`` so the validation
    integration tests can drive ``run_backtest`` end-to-end without a live
    data provider or plugin registry.
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


@pytest.fixture
def stub_kiq(monkeypatch):
    """Replace ``run_backtest.kiq`` with a configurable AsyncMock.

    Returns the mock so each test can set its ``return_value`` (a
    ``SimpleNamespace`` exposing ``task_id``) or its ``side_effect``.
    """
    mock = AsyncMock(name="run_backtest.kiq")
    monkeypatch.setattr(definitions.run_backtest, "kiq", mock)
    return mock


def _result_task(*, ready: bool, is_err: bool = False, return_value=None,
                 error=None, execution_time: float = 0.0):
    """Build a fake ``AsyncTaskiqTask`` for ``collect_backtest_result``."""
    task = MagicMock(name="AsyncTaskiqTask")
    task.is_ready = AsyncMock(return_value=ready)
    task.get_result = AsyncMock(
        return_value=SimpleNamespace(
            execution_time=execution_time,
            is_err=is_err,
            error=error,
            return_value=return_value,
        )
    )
    return task


# --------------------------------------------------------------------------- #
# _validate_backtest_inputs — the helper itself
# --------------------------------------------------------------------------- #
class TestValidateBacktestInputs:
    def test_valid_inputs_pass_through_unchanged(self):
        out = definitions._validate_backtest_inputs(
            strategy_name="momentum",
            symbol="AAPL",
            start_date="2023-01-01",
            end_date="2023-06-01",
            initial_capital=100_000.0,
        )
        assert out == ("momentum", "AAPL", "2023-01-01", "2023-06-01", 100_000.0)

    def test_int_capital_coerced_to_float(self):
        out = definitions._validate_backtest_inputs(
            strategy_name="s", symbol="A",
            start_date="2023-01-01", end_date="2023-06-01",
            initial_capital=50_000,
        )
        assert out[4] == 50_000.0
        assert isinstance(out[4], float)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_nan_inf_capital_rejected(self, bad):
        with pytest.raises(ValueError, match="initial_capital must be a finite number"):
            definitions._validate_backtest_inputs(
                strategy_name="s", symbol="A",
                start_date="2023-01-01", end_date="2023-06-01",
                initial_capital=bad,
            )

    def test_negative_capital_rejected(self):
        with pytest.raises(ValueError, match="initial_capital must not be negative"):
            definitions._validate_backtest_inputs(
                strategy_name="s", symbol="A",
                start_date="2023-01-01", end_date="2023-06-01",
                initial_capital=-1.0,
            )

    def test_zero_capital_is_allowed(self):
        # Zero is non-negative and finite, so it must be accepted.
        out = definitions._validate_backtest_inputs(
            strategy_name="s", symbol="A",
            start_date="2023-01-01", end_date="2023-06-01",
            initial_capital=0,
        )
        assert out[4] == 0.0

    def test_bool_capital_rejected(self):
        with pytest.raises(TypeError, match="initial_capital must be a number"):
            definitions._validate_backtest_inputs(
                strategy_name="s", symbol="A",
                start_date="2023-01-01", end_date="2023-06-01",
                initial_capital=True,
            )

    def test_non_number_capital_rejected(self):
        with pytest.raises(TypeError, match="initial_capital must be a number"):
            definitions._validate_backtest_inputs(
                strategy_name="s", symbol="A",
                start_date="2023-01-01", end_date="2023-06-01",
                initial_capital="100000",
            )

    def test_reversed_dates_rejected(self):
        with pytest.raises(ValueError, match="must be strictly before"):
            definitions._validate_backtest_inputs(
                strategy_name="s", symbol="A",
                start_date="2023-06-01", end_date="2023-01-01",
                initial_capital=100.0,
            )

    def test_equal_dates_rejected(self):
        # The window must be non-empty: start must be strictly before end.
        with pytest.raises(ValueError, match="must be strictly before"):
            definitions._validate_backtest_inputs(
                strategy_name="s", symbol="A",
                start_date="2023-01-01", end_date="2023-01-01",
                initial_capital=100.0,
            )

    def test_unparseable_date_rejected(self):
        with pytest.raises(ValueError, match="not a valid YYYY-MM-DD date"):
            definitions._validate_backtest_inputs(
                strategy_name="s", symbol="A",
                start_date="not-a-date", end_date="2023-06-01",
                initial_capital=100.0,
            )

    def test_non_string_date_rejected(self):
        with pytest.raises(TypeError, match="must be a string in YYYY-MM-DD format"):
            definitions._validate_backtest_inputs(
                strategy_name="s", symbol="A",
                start_date=20230101, end_date="2023-06-01",
                initial_capital=100.0,
            )

    def test_control_chars_stripped_from_strategy_and_symbol(self):
        # A NUL + tab + newline embedded in the free-text fields must be
        # removed, leaving the cleaned value usable downstream.
        out = definitions._validate_backtest_inputs(
            strategy_name="mom\x00ent\tum\n",
            symbol="AA\tPL\r",
            start_date="2023-01-01",
            end_date="2023-06-01",
            initial_capital=100.0,
        )
        assert out[0] == "momentum"
        assert out[1] == "AAPL"

    def test_control_char_injection_cannot_clear_the_value(self):
        # An all-control-character strategy name is empty after stripping,
        # which must be rejected so a payload can't be smuggled in.
        with pytest.raises(ValueError, match="strategy_name must not be empty"):
            definitions._validate_backtest_inputs(
                strategy_name="\x00\x01\x02",
                symbol="A",
                start_date="2023-01-01",
                end_date="2023-06-01",
                initial_capital=100.0,
            )

    def test_empty_symbol_rejected(self):
        with pytest.raises(ValueError, match="symbol must not be empty"):
            definitions._validate_backtest_inputs(
                strategy_name="s", symbol="   ",
                start_date="2023-01-01", end_date="2023-06-01",
                initial_capital=100.0,
            )

    def test_non_string_strategy_name_rejected(self):
        with pytest.raises(TypeError, match="strategy_name must be a string"):
            definitions._validate_backtest_inputs(
                strategy_name=None, symbol="A",
                start_date="2023-01-01", end_date="2023-06-01",
                initial_capital=100.0,
            )

    def test_non_string_symbol_rejected(self):
        with pytest.raises(TypeError, match="symbol must be a string"):
            definitions._validate_backtest_inputs(
                strategy_name="s", symbol=None,
                start_date="2023-01-01", end_date="2023-06-01",
                initial_capital=100.0,
            )

    def test_c1_and_del_control_chars_stripped(self):
        # DEL (0x7f) and C1 controls (0x80-0x9f) must also be stripped.
        out = definitions._validate_backtest_inputs(
            strategy_name="a\x7fb\x9fc",
            symbol="X",
            start_date="2023-01-01",
            end_date="2023-06-01",
            initial_capital=100.0,
        )
        assert out[0] == "abc"


# --------------------------------------------------------------------------- #
# run_backtest — validation wired into the public task
# --------------------------------------------------------------------------- #
class TestRunBacktestValidation:
    async def test_nan_capital_returns_failed_envelope(
        self, mock_engine, fast_retry_sleep
    ):
        payload = await definitions.run_backtest(
            "momentum", "AAPL", "2023-01-01", "2023-06-01",
            initial_capital=float("nan"),
        )
        assert payload["status"] == "failed"
        assert payload["error_type"] == "ValueError"
        assert "finite number" in payload["error"]
        # Validation fails before the engine is touched and before retries.
        mock_engine.runner.run.assert_not_awaited()
        assert fast_retry_sleep == []

    async def test_negative_capital_returns_failed_envelope(
        self, mock_engine, fast_retry_sleep
    ):
        payload = await definitions.run_backtest(
            "momentum", "AAPL", "2023-01-01", "2023-06-01",
            initial_capital=-500.0,
        )
        assert payload["status"] == "failed"
        assert payload["error_type"] == "ValueError"
        assert "negative" in payload["error"]
        assert fast_retry_sleep == []

    async def test_reversed_dates_returns_failed_envelope(
        self, mock_engine, fast_retry_sleep
    ):
        payload = await definitions.run_backtest(
            "momentum", "AAPL", start_date="2023-06-01", end_date="2023-01-01",
        )
        assert payload["status"] == "failed"
        assert payload["error_type"] == "ValueError"
        assert "strictly before" in payload["error"]
        assert fast_retry_sleep == []

    async def test_control_chars_cleaned_before_execution(
        self, mock_engine, fast_retry_sleep
    ):
        # The engine mock resolves whatever name it's asked for, so this
        # exercises the cleaning path: the raw name has a NUL injected but
        # the registry must be queried with the cleaned value and the run
        # must succeed.
        payload = await definitions.run_backtest(
            "mom\x00entum", "AA\tPL", "2023-01-01", "2023-06-01",
        )
        assert payload["status"] == "completed"
        mock_engine.registry_instance.load_strategy.assert_called_once_with("momentum")
        config = mock_engine.runner_cls.call_args.kwargs["config"]
        assert config.strategy_name == "momentum"
        assert config.symbol == "AAPL"
        # The completed envelope echoes the cleaned identifiers.
        assert payload["strategy_name"] == "momentum"
        assert payload["symbol"] == "AAPL"


# --------------------------------------------------------------------------- #
# submit_backtest_job — incl. the None task_id guard
# --------------------------------------------------------------------------- #
class TestSubmitBacktestJob:
    async def test_happy_path_returns_submitted_with_task_id(self, stub_kiq):
        stub_kiq.return_value = SimpleNamespace(task_id="job-123")
        payload = await definitions.submit_backtest_job(
            "momentum", "AAPL", "2023-01-01", "2023-06-01", initial_capital=1_000.0,
        )
        assert payload["status"] == "submitted"
        assert payload["task_id"] == "job-123"
        assert payload["strategy_name"] == "momentum"
        assert payload["initial_capital"] == 1_000.0
        stub_kiq.assert_awaited_once_with(
            "momentum", "AAPL", "2023-01-01", "2023-06-01", 1_000.0,
        )

    async def test_none_task_id_returns_failed_envelope(self, stub_kiq):
        # Broker accepted the enqueue but returned an object with no/None
        # task_id — must surface as a failed envelope, never as "submitted".
        stub_kiq.return_value = SimpleNamespace(task_id=None)
        payload = await definitions.submit_backtest_job(
            "momentum", "AAPL", "2023-01-01", "2023-06-01",
        )
        assert payload["status"] == "failed"
        assert payload["error_type"] == "RuntimeError"
        assert "no task_id" in payload["error"]
        assert payload["strategy_name"] == "momentum"
        assert payload["symbol"] == "AAPL"
        assert payload["correlation_id"] is not None

    async def test_missing_task_id_attribute_returns_failed_envelope(self, stub_kiq):
        # Defensive: a broker result object that lacks the attribute entirely
        # must also be caught (getattr defaults to None).
        stub_kiq.return_value = SimpleNamespace()  # no task_id at all
        payload = await definitions.submit_backtest_job(
            "momentum", "AAPL", "2023-01-01", "2023-06-01",
        )
        assert payload["status"] == "failed"
        assert payload["error_type"] == "RuntimeError"
        assert "no task_id" in payload["error"]

    async def test_broker_reject_returns_failed_envelope(self, stub_kiq):
        stub_kiq.side_effect = ConnectionError("redis down")
        payload = await definitions.submit_backtest_job(
            "momentum", "AAPL", "2023-01-01", "2023-06-01",
        )
        assert payload["status"] == "failed"
        assert payload["error_type"] == "ConnectionError"
        assert "redis down" in payload["error"]

    async def test_invalid_capital_returns_failed_envelope(self, stub_kiq):
        payload = await definitions.submit_backtest_job(
            "momentum", "AAPL", "2023-01-01", "2023-06-01",
            initial_capital=float("inf"),
        )
        assert payload["status"] == "failed"
        assert payload["error_type"] == "ValueError"
        assert "finite number" in payload["error"]
        # Validation happens before the enqueue, so the broker is untouched.
        stub_kiq.assert_not_awaited()

    async def test_reversed_dates_returns_failed_envelope(self, stub_kiq):
        payload = await definitions.submit_backtest_job(
            "momentum", "AAPL", "2023-06-01", "2023-01-01",
        )
        assert payload["status"] == "failed"
        assert payload["error_type"] == "ValueError"
        assert "strictly before" in payload["error"]
        stub_kiq.assert_not_awaited()

    async def test_control_chars_cleaned_before_enqueue(self, stub_kiq):
        stub_kiq.return_value = SimpleNamespace(task_id="job-9")
        payload = await definitions.submit_backtest_job(
            "mom\x00entum", "AA\tPL", "2023-01-01", "2023-06-01",
        )
        assert payload["status"] == "submitted"
        # The cleaned values are what gets enqueued + echoed.
        assert payload["strategy_name"] == "momentum"
        assert payload["symbol"] == "AAPL"
        stub_kiq.assert_awaited_once_with(
            "momentum", "AAPL", "2023-01-01", "2023-06-01", 100_000.0,
        )


# --------------------------------------------------------------------------- #
# collect_backtest_result — backend interaction
# --------------------------------------------------------------------------- #
class TestCollectBacktestResult:
    async def test_pending(self, monkeypatch):
        monkeypatch.setattr(
            definitions, "_build_result_task",
            lambda task_id: _result_task(ready=False),
        )
        payload = await definitions.collect_backtest_result("abc")
        assert payload["status"] == "pending"
        assert payload["task_id"] == "abc"
        assert payload["correlation_id"] is not None

    async def test_completed(self, monkeypatch):
        monkeypatch.setattr(
            definitions, "_build_result_task",
            lambda task_id: _result_task(
                ready=True, is_err=False,
                return_value={"status": "completed", "final_capital": 99.0},
                execution_time=1.25,
            ),
        )
        payload = await definitions.collect_backtest_result("abc")
        assert payload["status"] == "completed"
        assert payload["execution_time"] == 1.25
        assert payload["result"]["final_capital"] == 99.0

    async def test_task_error_envelope(self, monkeypatch):
        boom = RuntimeError("worker blew up")
        monkeypatch.setattr(
            definitions, "_build_result_task",
            lambda task_id: _result_task(
                ready=True, is_err=True, error=boom, execution_time=0.5,
            ),
        )
        payload = await definitions.collect_backtest_result("abc")
        assert payload["status"] == "failed"
        assert payload["error_type"] == "RuntimeError"
        assert "worker blew up" in payload["error"]
        assert payload["execution_time"] == 0.5

    async def test_is_ready_backend_failure_returns_failed(self, monkeypatch):
        broken = MagicMock()
        broken.is_ready = AsyncMock(side_effect=ConnectionError("redis gone"))
        monkeypatch.setattr(
            definitions, "_build_result_task", lambda task_id: broken,
        )
        payload = await definitions.collect_backtest_result("abc")
        assert payload["status"] == "failed"
        assert payload["error_type"] == "ConnectionError"
        assert "redis gone" in payload["error"]

    async def test_get_result_failure_returns_failed(self, monkeypatch):
        broken = MagicMock()
        broken.is_ready = AsyncMock(return_value=True)
        broken.get_result = AsyncMock(side_effect=RuntimeError("decode error"))
        monkeypatch.setattr(
            definitions, "_build_result_task", lambda task_id: broken,
        )
        payload = await definitions.collect_backtest_result("abc")
        assert payload["status"] == "failed"
        assert payload["error_type"] == "RuntimeError"
        assert "decode error" in payload["error"]
