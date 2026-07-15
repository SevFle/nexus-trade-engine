"""Tests for ``run_strategy_evaluation`` and friends in ``definitions``.

``run_strategy_evaluation`` (engine.tasks.definitions lines ~492-542) was the
single largest block of *completely untested* code in ``definitions.py`` after
the input-validation work landed: it loads a strategy from the plugin
registry, rebuilds a :class:`nexus_sdk.MarketState` from a JSON-serialisable
dict, awaits ``strategy.evaluate`` (via the retried
:func:`_evaluate_strategy` helper) and serialises the emitted signals back to
dicts so the result survives the Redis result backend.

These tests exercise every branch of that flow:

* the happy path with pydantic ``Signal`` objects (``model_dump`` branch),
* plain-dict signals (the ``dict(s)`` fallback branch),
* an empty signal list and a ``None`` return (coerced to empty),
* an unknown strategy → ``ValueError`` → ``failed`` envelope,
* an invalid ``market_state`` → pydantic ``ValidationError`` → ``failed`` envelope,
* ``strategy.evaluate`` raising → ``failed`` envelope,
* default ``market_state``/``portfolio``/``costs`` (all ``None``) → ``{}``,
* correct threading of the rebuilt ``MarketState`` + ``portfolio``/``costs``
  into ``strategy.evaluate``, and JSON-serialisability of the envelope.

They also cover the two remaining trivial-but-uncovered helpers:

* :func:`_evaluate_strategy` — the retried ``strategy.evaluate`` wrapper,
* :func:`_build_result_task` — binds a ``task_id`` to the broker's result
  backend.

All external dependencies (plugin registry, the retry sleeper) are mocked so
the tests are deterministic and never touch Redis/Valkey or the network.
"""

from __future__ import annotations

import json
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
    """Make ``with_retry`` backoff sleeps instantaneous and observable.

    ``_evaluate_strategy`` is decorated with ``@with_retry``; making the
    sleeper instant keeps the tests fast and lets us assert no retry
    happened on the success path.
    """
    delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(definitions, "_retry_sleep", _fake_sleep)
    return delays


@pytest.fixture
def registry(monkeypatch):
    """Patch the lazily-imported ``PluginRegistry`` used by the task.

    Returns the mock registry instance so each test can wire
    ``load_strategy`` to return a strategy (or ``None``) and set the
    strategy's ``evaluate`` coroutine.
    """
    strategy = MagicMock(name="strategy")
    strategy.evaluate = AsyncMock(return_value=[])

    registry_instance = MagicMock(name="PluginRegistry-instance")
    registry_instance.load_strategy.return_value = strategy
    registry_cls = MagicMock(return_value=registry_instance)

    monkeypatch.setattr("engine.plugins.registry.PluginRegistry", registry_cls)
    return SimpleNamespace(
        cls=registry_cls,
        instance=registry_instance,
        strategy=strategy,
    )


def _signal(symbol: str = "AAPL", side: str = "buy"):
    """Build a real pydantic ``Signal`` so ``model_dump`` is exercised."""
    from nexus_sdk import Signal
    from nexus_sdk.signals import Side

    return Signal(symbol=symbol, side=Side(side), strategy_id="momentum")


# --------------------------------------------------------------------------- #
# run_strategy_evaluation — happy paths
# --------------------------------------------------------------------------- #
class TestRunStrategyEvaluationHappyPath:
    async def test_pydantic_signals_serialised_via_model_dump(self, registry, fast_retry_sleep):
        buy, sell = _signal("AAPL", "buy"), _signal("MSFT", "sell")
        registry.strategy.evaluate.return_value = [buy, sell]

        payload = await definitions.run_strategy_evaluation(
            "momentum",
            market_state={"prices": {"AAPL": 150.0, "MSFT": 330.0}},
            portfolio={"cash": 1000.0},
            costs={"commission": 1.0},
        )

        assert payload["status"] == "completed"
        assert payload["strategy_name"] == "momentum"
        assert payload["signal_count"] == 2
        assert len(payload["signals"]) == 2
        # model_dump(mode="json") turns the StrEnum side into a plain string.
        assert payload["signals"][0]["side"] == "buy"
        assert payload["signals"][0]["symbol"] == "AAPL"
        assert payload["signals"][1]["side"] == "sell"
        # Each entry is a dict (JSON-serialisable form), not a Signal object.
        assert all(isinstance(s, dict) for s in payload["signals"])
        assert payload["correlation_id"] is not None
        # Success path never retries.
        assert fast_retry_sleep == []

    async def test_plain_dict_signals_use_dict_fallback(self, registry):
        # Signals lacking ``model_dump`` take the ``dict(s)`` branch.
        dict_signals = [
            {"symbol": "AAPL", "action": "buy"},
            {"symbol": "TSLA", "action": "hold"},
        ]
        registry.strategy.evaluate.return_value = dict_signals

        payload = await definitions.run_strategy_evaluation("momentum")

        assert payload["status"] == "completed"
        assert payload["signal_count"] == 2
        # dict() copies the mapping verbatim.
        assert payload["signals"] == dict_signals

    async def test_empty_signal_list_returns_completed(self, registry):
        registry.strategy.evaluate.return_value = []

        payload = await definitions.run_strategy_evaluation("momentum")

        assert payload["status"] == "completed"
        assert payload["signal_count"] == 0
        assert payload["signals"] == []

    async def test_none_signals_coerced_to_empty(self, registry):
        # ``(raw_signals or [])`` must treat a None return as no signals.
        registry.strategy.evaluate.return_value = None

        payload = await definitions.run_strategy_evaluation("momentum")

        assert payload["status"] == "completed"
        assert payload["signal_count"] == 0
        assert payload["signals"] == []

    async def test_completed_payload_is_json_serialisable(self, registry):
        registry.strategy.evaluate.return_value = [_signal("AAPL", "buy")]

        payload = await definitions.run_strategy_evaluation("momentum")

        # The envelope round-trips through the JSON result backend.
        round_tripped = json.loads(json.dumps(payload))
        assert round_tripped["status"] == "completed"
        assert round_tripped["signal_count"] == 1


# --------------------------------------------------------------------------- #
# run_strategy_evaluation — default arguments threading
# --------------------------------------------------------------------------- #
class TestRunStrategyEvaluationArgumentThreading:
    async def test_defaults_pass_empty_dicts(self, registry):
        # ``market_state`` and ``costs`` coalesce to ``{}`` when omitted
        # (``market_state or {}`` / ``costs or {}``); ``portfolio`` keeps its
        # signature default of ``None`` and is forwarded verbatim.
        registry.strategy.evaluate.return_value = []

        await definitions.run_strategy_evaluation("momentum")

        registry.instance.load_strategy.assert_called_once_with("momentum")
        args = registry.strategy.evaluate.await_args
        # evaluate() forwards its three positional args as portfolio, market, costs
        portfolio, market, costs = args.args
        assert portfolio is None
        assert costs == {}
        # market_state=None -> {} -> an (empty, valid) MarketState.
        assert market.prices == {}

    async def test_market_state_rebuilt_and_passed_to_evaluate(self, registry):
        from nexus_sdk import MarketState

        registry.strategy.evaluate.return_value = []

        await definitions.run_strategy_evaluation(
            "momentum",
            market_state={"prices": {"AAPL": 150.0}},
        )

        _, market, _ = registry.strategy.evaluate.await_args.args
        assert isinstance(market, MarketState)
        assert market.prices == {"AAPL": 150.0}

    async def test_portfolio_and_costs_forwarded_verbatim(self, registry):
        registry.strategy.evaluate.return_value = []

        portfolio = {"cash": 42.0, "positions": {"AAPL": {"qty": 10}}}
        costs = {"commission": 0.5, "slippage": 0.1}

        await definitions.run_strategy_evaluation(
            "momentum",
            portfolio=portfolio,
            costs=costs,
        )

        forwarded_portfolio, _, forwarded_costs = registry.strategy.evaluate.await_args.args
        assert forwarded_portfolio == portfolio
        assert forwarded_costs == costs


# --------------------------------------------------------------------------- #
# run_strategy_evaluation — error / failure envelopes
# --------------------------------------------------------------------------- #
class TestRunStrategyEvaluationFailures:
    async def test_unknown_strategy_returns_failed_envelope(self, registry):
        registry.instance.load_strategy.return_value = None

        payload = await definitions.run_strategy_evaluation("ghost-strategy")

        assert payload["status"] == "failed"
        assert payload["strategy_name"] == "ghost-strategy"
        assert payload["error_type"] == "ValueError"
        assert "ghost-strategy" in payload["error"]
        assert payload["correlation_id"] is not None
        # evaluate is never reached.
        registry.strategy.evaluate.assert_not_awaited()

    async def test_invalid_market_state_returns_failed_envelope(self, registry):
        # ``prices`` must be a dict[str, float]; a scalar is rejected.
        payload = await definitions.run_strategy_evaluation(
            "momentum",
            market_state={"prices": 123},
        )

        assert payload["status"] == "failed"
        assert payload["strategy_name"] == "momentum"
        # pydantic raises ValidationError (a ValueError subclass).
        assert payload["error_type"] in {"ValidationError", "ValueError"}
        assert payload["correlation_id"] is not None
        registry.strategy.evaluate.assert_not_awaited()

    async def test_evaluate_raising_returns_failed_envelope(self, registry, fast_retry_sleep):
        # A non-retryable error from evaluate surfaces as a failed envelope.
        registry.strategy.evaluate.side_effect = ValueError("bad config")

        payload = await definitions.run_strategy_evaluation("momentum")

        assert payload["status"] == "failed"
        assert payload["error_type"] == "ValueError"
        assert "bad config" in payload["error"]

    async def test_failed_envelope_is_json_serialisable(self, registry):
        registry.instance.load_strategy.return_value = None

        payload = await definitions.run_strategy_evaluation("ghost")

        # Even the failure envelope must survive the JSON result backend.
        json.loads(json.dumps(payload))


# --------------------------------------------------------------------------- #
# _evaluate_strategy — the retried wrapper (covers line 285)
# --------------------------------------------------------------------------- #
class TestEvaluateStrategyHelper:
    async def test_calls_strategy_evaluate_with_kwargs(self, fast_retry_sleep):
        strategy = MagicMock(name="strategy")
        strategy.evaluate = AsyncMock(return_value=["sig"])

        market, portfolio, costs = object(), object(), object()
        out = await definitions._evaluate_strategy(
            strategy=strategy,
            market=market,
            portfolio=portfolio,
            costs=costs,
        )

        assert out == ["sig"]
        strategy.evaluate.assert_awaited_once_with(portfolio, market, costs)
        assert fast_retry_sleep == []

    async def test_retries_transient_failure_then_succeeds(self, fast_retry_sleep):
        strategy = MagicMock(name="strategy")
        strategy.evaluate = AsyncMock(side_effect=[ConnectionError("blip"), ["sig"]])

        out = await definitions._evaluate_strategy(
            strategy=strategy,
            market=None,
            portfolio=None,
            costs={},
        )

        assert out == ["sig"]
        # one transient failure -> exactly one backoff sleep before the retry
        assert len(fast_retry_sleep) == 1

    async def test_non_retryable_error_propagates_immediately(self, fast_retry_sleep):
        strategy = MagicMock(name="strategy")
        strategy.evaluate = AsyncMock(side_effect=ValueError("boom"))

        with pytest.raises(ValueError, match="boom"):
            await definitions._evaluate_strategy(
                strategy=strategy,
                market=None,
                portfolio=None,
                costs={},
            )
        # No retry scheduled for a permanent error.
        assert fast_retry_sleep == []


# --------------------------------------------------------------------------- #
# _build_result_task — binds a task_id to the broker result backend (line 576)
# --------------------------------------------------------------------------- #
class TestBuildResultTask:
    def test_binds_task_id_to_broker_result_backend(self):
        from taskiq import AsyncTaskiqTask

        task = definitions._build_result_task("job-42")

        assert isinstance(task, AsyncTaskiqTask)
        assert task.task_id == "job-42"
        assert task.result_backend is definitions.broker.result_backend


# --------------------------------------------------------------------------- #
# Lifecycle hooks — previously uncovered (lines 184-189, 206-207)
# --------------------------------------------------------------------------- #
class TestLifecycleHooks:
    async def test_on_worker_startup_logs_registered_tasks(self):
        # The hook must complete without error and tolerate the broker's
        # ``get_all_tasks`` implementation.
        await definitions.on_worker_startup(None)

    async def test_on_worker_startup_tolerates_broker_error(self, monkeypatch):
        # A misbehaving broker.get_all_tasks must not crash startup.
        monkeypatch.setattr(
            definitions.broker,
            "get_all_tasks",
            MagicMock(side_effect=RuntimeError("broker impl detail")),
        )
        await definitions.on_worker_startup(None)

    async def test_on_worker_shutdown_logs_ordered_shutdown(self):
        await definitions.on_worker_shutdown(None)
