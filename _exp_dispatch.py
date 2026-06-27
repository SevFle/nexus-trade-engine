"""Scratch experiment: probe real orchestrator dispatch for each edge case."""
from __future__ import annotations

import asyncio
import functools
import types
import warnings

from engine.core.signal import Side, Signal
from engine.core.strategy_orchestrator import StrategyOrchestrator


def _sig(symbol, side, sid):
    return Signal(symbol=symbol, side=side, strategy_id=sid)


M = {"p": {"AAPL": 1.0}}
C = {"f": 1.0}


# ----------------------------------------------------------------- #
# Pattern 1: callable class instance with async __call__ used AS evaluate
# ----------------------------------------------------------------- #
class _AsyncCallableEvaluator:
    async def __call__(self, market_data, cost_model):
        return [_sig("AAPL", Side.BUY, "async-call")]


class AsyncCallStrategy:
    id = "async-call"

    def __init__(self):
        self.evaluate = _AsyncCallableEvaluator()


# Variant 1b: strategy object itself is async-callable, no evaluate attr
class AsyncCallableObject:
    id = "async-call-obj"

    async def __call__(self, market_data, cost_model):
        return [_sig("AAPL", Side.BUY, "async-call-obj")]


# ----------------------------------------------------------------- #
# Pattern 2: functools.partial-wrapped coroutine
# ----------------------------------------------------------------- #
async def _async_fn(market_data, cost_model, factor):
    return [_sig("AAPL", Side.BUY, "partial")]


class PartialStrategy:
    id = "partial"

    def __init__(self):
        self.evaluate = functools.partial(_async_fn, factor=2)


# ----------------------------------------------------------------- #
# Pattern 3: sync method that returns a coroutine object
# ----------------------------------------------------------------- #
async def _make_coro(market_data, cost_model):
    return [_sig("AAPL", Side.BUY, "sync-ret-coro")]


class SyncReturningCoroutineStrategy:
    id = "sync-ret-coro"

    def evaluate(self, market_data, cost_model):  # sync def
        return _make_coro(market_data, cost_model)


# ----------------------------------------------------------------- #
# Pattern 4: legacy generator-based coroutine via types.coroutine
# (asyncio.coroutine was removed in 3.11; types.coroutine is the
# documented way to build a generator-based coroutine)
# ----------------------------------------------------------------- #
@types.coroutine
def _legacy_coro(market_data, cost_model):
    yield
    return [_sig("AAPL", Side.BUY, "legacy")]


class LegacyStrategy:
    id = "legacy"

    def evaluate(self, market_data, cost_model):
        return _legacy_coro(market_data, cost_model)


async def run_case(name, strategy_factory):
    orch = StrategyOrchestrator()
    strat = strategy_factory()
    # Some variants have no `evaluate` — register may raise.
    try:
        orch.register(strat)
    except Exception as exc:
        print(f"[{name}] REGISTER raised: {type(exc).__name__}: {exc}")
        return
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            result = await orch.evaluate_all(M, C)
            sides = {s.symbol: s.side for s in result.signals}
            errs = result.errors
            warns = [
                str(w.message)
                for w in caught
                if "coroutine" in str(w.message).lower()
                or "never awaited" in str(w.message).lower()
            ]
            print(f"[{name}] signals={sides} errors={errs} coro_warns={warns}")
        except Exception as exc:
            print(f"[{name}] evaluate_all raised: {type(exc).__name__}: {exc}")


async def main():
    await run_case("async __call__ as evaluate", AsyncCallStrategy)
    await run_case("async __call__ object (no evaluate)", AsyncCallableObject)
    await run_case("partial coroutine", PartialStrategy)
    await run_case("sync returns coroutine", SyncReturningCoroutineStrategy)
    await run_case("legacy generator coroutine", LegacyStrategy)


asyncio.run(main())
