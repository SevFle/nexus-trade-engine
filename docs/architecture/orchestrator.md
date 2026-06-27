# Strategy orchestrator (multi-strategy)

The [`StrategyOrchestrator`](../../engine/core/strategy_orchestrator.py)
runs many strategies against the *same* market data and cost model in a
single evaluation cycle, then merges their emitted signals into one
decision set. It is the multi-strategy analogue of the single-strategy
[`StrategyEvaluator`](../../engine/core/strategy_evaluator.py) and is
the component the README's "AI-native plugin trading framework" pitch
eventually rests on.

> **Status.** The orchestrator is **landed as a library, not yet wired
> to a route, worker, or evaluator**. It has no caller outside its own
> unit tests; nothing in `engine/api/`, `engine/tasks/`, or the live
> loop constructs or invokes it. It is on the roadmap as
> "StrategyOrchestrator (multi-strategy) [partial]" — see
> [known-limitations.md](../known-limitations.md). Treat this doc as
> the design contract for the code that *does* exist, not as a guide
> to a running feature.

## Why a separate component

Nexus already had a [`SignalAggregator`](../../engine/core/signal_aggregator.py)
(gh#21) that resolves conflicting per-strategy signals per symbol
(majority / weighted, with HOLD-as-abstain). The orchestrator does
**not** reimplement that voting math. It owns three responsibilities
the aggregator deliberately doesn't:

1. **Registry** — strategies are registered with a per-strategy
   `weight` that the `weighted` aggregation mode consults.
2. **Evaluation** — every registered strategy is invoked with the
   *same* `market_data` and `cost_model`, so cross-strategy
   comparisons stay apples-to-apples. A single failing or
   slow strategy is isolated; its error is recorded and the
   remaining strategies still contribute to the vote.
3. **Aggregation dispatch** — the collected per-strategy
   `SignalBatch` objects are handed to `SignalAggregator`, which
   already implements the per-symbol majority/weighted resolution.
   Reusing it keeps a single source of truth for tie handling.

## Aggregation modes

| Mode | Alias | Behaviour |
|---|---|---|
| `majority` | — | Default. Each strategy that takes a position (BUY/SELL) casts one equal vote. A side wins only if it takes *strictly more than half* of the BUY-vs-SELL votes; a tie (e.g. 1 BUY vs 1 SELL) emits HOLD. HOLD signals abstain and do not count toward the denominator. |
| `majority_vote` | (same as `majority`) | Ergonomic alias matching the mode name used in design docs. Collapses onto the same one-vote-per-strategy majority. |
| `weighted` | — | Each strategy's vote is multiplied by its registered weight (default 1.0). The side with the *strictly higher* total weight wins; a tie emits HOLD. This lets a high-conviction strategy override a numerical majority. |

Both modes return HOLD for a symbol when no strategy expresses an
opinion beyond HOLD, so downstream consumers always get a record that
the symbol was considered.

> **Weighted + all-zero weights is a misconfiguration**, not a silent
> no-op: `SignalAggregator` raises `SignalAggregatorError`. The
> orchestrator does not catch it — the caller asked for weighted
> resolution and made it impossible, so the failure should surface.

## The contract a strategy must satisfy

The orchestrator is structural-typed (`Protocol`), so it accepts
anything with the right shape rather than requiring a specific base
class:

```python
class StrategyLike(Protocol):
    @property
    def id(self) -> str: ...
    def evaluate(
        self, market_data: Any, cost_model: Any
    ) -> list[Signal] | Awaitable[list[Signal]]: ...
```

- `id` may be a string attribute **or** a no-arg callable/property.
  The orchestrator resolves it via `_strategy_id()` and raises
  `StrategyOrchestratorError` if it isn't a non-empty string.
- `evaluate` may be sync **or** async. The orchestrator awaits
  awaitable results transparently — only the async path is bounded by
  the per-strategy timeout (see below); a sync call has already
  completed by the time we get the return value.

This intentionally matches the engine's in-engine `BaseStrategy`
shape and is close enough to the public SDK `IStrategy` that an
adapter can bridge them.

## Invariants enforced at the cycle boundary

Three hardening guarantees landed in the orchestrator's first
review cycle (commit `578259b`) after the initial implementation
proved fragile under concurrency. They are worth calling out because
each maps to a specific failure mode the design rejects:

1. **Registry snapshot before iteration.** `evaluate_all` walks
   `list(self._strategies.items())` — a *copy* of the registry — so a
   strategy that registers or unregisters a sibling mid-cycle (e.g.
   from inside its own `evaluate`) cannot trigger
   `RuntimeError: dictionary changed size during iteration`.
   Strategies added during this cycle are intentionally excluded;
   they run on the next.
2. **Per-strategy `asyncio.wait_for` timeout.** Each async
   `evaluate()` is wrapped in `asyncio.wait_for(..., timeout=eval_timeout)`
   (default 30 s — matches the platform's overall strategy SLA).
   A strategy that blows the budget is recorded in `errors` as a
   `TimeoutError` entry rather than stalling the whole cycle. Tighten
   via the constructor for latency-sensitive paths.
3. **Deep-copied inputs per strategy.** Every strategy receives its
   own `copy.deepcopy(market_data)` and `copy.deepcopy(cost_model)`,
   recreated per strategy. A misbehaving plugin that mutates them in
   place cannot poison its siblings or the caller's originals, and
   cross-strategy comparisons stay apples-to-apples.

A strategy that raises (anything other than `TimeoutError`) is
logged at `exception` level, recorded in `errors`, and excluded
from the vote — it does **not** abort the cycle.

## Result shape

`evaluate_all()` returns an `OrchestrationResult`, intentionally
richer than a bare `list[Signal]`:

| Field | Purpose |
|---|---|
| `signals` | The aggregated decision set (one `Signal` per considered symbol). |
| `batches` | Full per-strategy provenance — one `SignalBatch` per strategy that ran successfully. Keep for audit/traceability. |
| `aggregation` | The resolved mode string (`majority` / `weighted`). |
| `strategy_count` | Registry size at cycle start (snapshot, not the live count). |
| `weights` | Snapshot of the per-strategy weights used. |
| `errors` | `{strategy_id: "<ExcType>: <msg>"}` for every strategy that raised **or** timed out. A misbehaving plugin can never silently disappear from the record. |

Two conveniences:

- `result.trade_signals` — the subset of `signals` with `side != HOLD`.
- `result.is_noop` — `True` when no aggregated signal was produced at
  all (empty registry, or every strategy returned no signals).

## API sketch

```python
from engine.core.strategy_orchestrator import (
    AggregationMode,
    StrategyOrchestrator,
)

orch = StrategyOrchestrator(eval_timeout=30.0)
orch.register(mean_reversion, weight=1.0)
orch.register(momentum,       weight=2.0)   # 2x vote in weighted mode
orch.register(risk_guard,     weight=0.5)

result = await orch.evaluate_all(
    market_data=md,
    cost_model=cm,
    aggregation=AggregationMode.WEIGHTED.value,
)
# result.signals   → aggregated decisions
# result.errors    → {} on a clean cycle
```

`register()` re-registering an id updates both the instance and the
weight, and logs a warning so the overwrite is never silent.
`unregister()` returns `True`/`False` for whether the id was present.
`__len__` / `__contains__` / `strategy_ids` / `weights` /
`get_weight()` are the read-side introspection.

## What's missing

See [known-limitations.md](../known-limitations.md) for the tracked
"orchestrator not wired to a route" item. Concretely, turning this
into a running feature needs:

1. A construct site — either the live loop (when
   [`engine/core/live/`](../../engine/core/live/) lands a runner) or a
   new `/api/v1/orchestrator/...` route that builds an orchestrator
   from a portfolio's `installed_strategies`.
2. Persistence of `OrchestrationResult.batches` / `.errors` for audit
   (today the result is ephemeral).
3. A strategy-evaluator integration so an orchestrator-driven run can
   be scored the same way a single-strategy backtest is.

Until then, the orchestrator is the *implementation* of multi-strategy
voting; the integration gap is the call site that doesn't exist yet.
