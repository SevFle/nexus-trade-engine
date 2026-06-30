"""Deeper probe: detection semantics + forced async-callable-object path."""
# ruff: noqa: T201, SLF001, ARG001, ARG002
# Experimental scratch probe: intentional console diagnostics, deliberate
# probing of private orchestrator state, and interface-conforming args.
from __future__ import annotations

import asyncio
import inspect
import types
import warnings

from engine.core.signal import Side, Signal
from engine.core.strategy_orchestrator import StrategyOrchestrator


def _sig(symbol, side, sid):
    return Signal(symbol=symbol, side=side, strategy_id=sid)


M = {"p": {"AAPL": 1.0}}
C = {"f": 1.0}


class AsyncCallableEvaluator:
    async def __call__(self, market_data, cost_model):
        return [_sig("AAPL", Side.BUY, "x")]


class AsyncCallStrategy:
    id = "async-call"
    def __init__(self):
        self.evaluate = AsyncCallableEvaluator()


# Bare async-callable object (no evaluate)
class AsyncCallableObject:
    id = "aco"
    async def __call__(self, market_data, cost_model):
        return [_sig("AAPL", Side.BUY, "aco")]


print("== detection semantics ==")
s = AsyncCallStrategy()
print("evaluate is instance:", not inspect.isfunction(s.evaluate))
print("iscoroutinefunction(evaluate):", inspect.iscoroutinefunction(s.evaluate))
print("iscoroutinefunction(evaluate.__call__):", inspect.iscoroutinefunction(s.evaluate.__call__))
raw = s.evaluate(M, C)
print("isawaitable(raw):", inspect.isawaitable(raw))
raw.close()


async def forced_register():
    """Force-register an async-callable object by stuffing it past the check."""
    orch = StrategyOrchestrator()
    obj = AsyncCallableObject()
    # Bypass register()'s evaluate check to reach dispatch.
    orch._strategies["aco"] = obj
    orch._weights["aco"] = 1.0
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            result = await orch.evaluate_all(M, C)
            print("forced: signals=", {x.symbol: x.side for x in result.signals})
            print("forced: errors=", result.errors)
        except Exception as exc:
            print("forced: raised", type(exc).__name__, exc)
        cw = [str(w.message) for w in caught if "coroutine" in str(w.message).lower()]
        print("forced: coro warnings=", cw)


asyncio.run(forced_register())


print("== legacy generator-based coroutine detection ==")
@types.coroutine
def gen_coro(md, cm):
    yield
    return [_sig("AAPL", Side.BUY, "g")]


print("iscoroutinefunction(gen_coro):", inspect.iscoroutinefunction(gen_coro))
g = gen_coro(M, C)
print("isawaitable(g):", inspect.isawaitable(g))
print("iscoroutine(g):", inspect.iscoroutine(g))
g.close()
