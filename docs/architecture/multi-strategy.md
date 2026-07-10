# Multi-strategy orchestration

[`overview.md`](overview.md) describes the engine as a service;
[`core-domains.md`](core-domains.md) maps the domain layer. This page is
the deep dive on **one** cross-cutting concern: how the engine turns
*N* strategies' signals into one decision set, and how that decision set
is reconciled against a capital policy over time.

It was split out of `core-domains.md` because the story now spans
**four** orchestrators plus a drift-rebalancer — enough that it earns its
own page, and enough that keeping it inline pushed the domain doc over
the 500-line cap. Everything here is library-only today (no public run
route drives any of it); see [Status](#status) and
[`known-limitations.md`](../known-limitations.md).

## The four orchestrators at a glance

They overlap in spirit but are deliberately separate modules with
different conflict-resolution semantics — and, in two cases, different
*responsibilities*. Pick by **voting model** *and* by whether you need
capital allocation owned for you, not by file name.

| Module | Class | Conflict rule | Knows about money? | Async? |
|---|---|---|---|---|
| [`engine/orchestration/orchestrator.py`](../../engine/orchestration/orchestrator.py) | `StrategyOrchestrator` | `PRIORITY` / `NET_POSITION` | No (unitless weight) | two-step `run_all` + `aggregate_signals` |
| [`engine/core/strategy_orchestrator.py`](../../engine/core/strategy_orchestrator.py) | async orchestrator | `MAJORITY` / `WEIGHTED` | No (unitless weight) | yes, per-strategy timeout |
| [`engine/portfolio/orchestrator.py`](../../engine/portfolio/orchestrator.py) | `StrategyOrchestrator` | net weighted vote | No (unitless weight) | yes (transparent sync/async) |
| [`engine/portfolio/multi_strategy.py`](../../engine/portfolio/multi_strategy.py) | `MultiStrategyPortfolio` | `RISK_ADJUSTED` (capital-weighted) | **Yes** (dollar book) | yes, per-strategy timeout |

The unifying contract across all four: **HOLD abstains**. A strategy
that declines to vote never blocks the others, and a symbol on which
every strategy abstains still yields a single HOLD record so downstream
consumers know it was considered.

---

## `engine/orchestration/orchestrator.py` — `StrategyOrchestrator`

The "register N strategies, run them all, collapse to one decision set"
loop. Two-step API: `await orch.run_all(market)` collects every
strategy's signals, then `orch.aggregate_signals()` resolves conflicts.

`ConflictResolution` selects the merge rule:

| Mode | Rule |
|---|---|
| `PRIORITY` *(default)* | The highest-priority strategy with a non-HOLD opinion wins. Opposing signals from strategies tied at top priority → HOLD (stalemate). HOLD abstains. |
| `NET_POSITION` | `BUY = +weight`, `SELL = −weight` summed per symbol. Positive net → BUY, negative → SELL, zero → HOLD. Resolved weight is the net magnitude clamped to `[0, 1]`, so conviction can override headcount. *(Unique to this orchestrator.)* |

## `engine/core/strategy_orchestrator.py` — async orchestrator

The heavier async counterpart. Three responsibilities that the bare
[`SignalAggregator`](../../engine/core/signal_aggregator.py) (gh#21) does
not own:

1. **Registry** — each strategy is registered with a per-strategy
   `weight`.
2. **Evaluation** — every registered strategy sees the *same*
   `market_data` and `cost_model` so cross-strategy comparisons are
   apples-to-apples. A single failing strategy is isolated: its error is
   recorded and the rest still vote.
3. **Dispatch** — hands the per-strategy `SignalBatch`es to
   `SignalAggregator`, which is the single source of truth for tie
   handling.

Aggregation modes (in [`signal_aggregator.py`](../../engine/core/signal_aggregator.py)):

| Mode | Rule |
|---|---|
| `MAJORITY` | Strictly more than half of BUY-vs-SELL votes wins; tie → HOLD. HOLD abstains and is excluded from the denominator. |
| `WEIGHTED` | Vote × registered weight (default 1.0); strictly higher total wins; tie → HOLD. Lets a high-conviction strategy override a numerical majority. |

## `engine/portfolio/orchestrator.py` — `StrategyOrchestrator` (lightweight weighted vote)

> Added in `feat(portfolio): implement StrategyOrchestrator to unify
> signals` (95ba0fa). The fourth orchestrator.

The small, obvious counterpart to the two above. Register once with a
list of `(strategy, weight)` tuples; call `await evaluate(market_context)`
each cycle; get back a single [`SignalSet`](#signalset). It exists
because the async orchestrator and the priority/net orchestrator are
both heavier than the common case needs: most callers just want
"run my strategies on the same context, net the weighted votes, hand me
one decision per symbol with a provenance trail."

### Conflict resolution — net weighted vote

Per symbol, `BUY` casts `+weight`, `SELL` casts `−weight`, `HOLD`
abstains:

- Strictly positive net → `BUY`; strictly negative → `SELL`.
- **Exact tie (net == 0) → `HOLD`** ("strongest net weight wins"; a
  high-conviction strategy can outvote a numerical majority).
- Resolved `Signal.weight` is `min(|net|, 1.0)` — `Signal.weight` lives
  in `[0, 1]`, so the magnitude is clamped, never the side.

This is the same arithmetic as the priority orchestrator's
`NET_POSITION` mode and the async orchestrator's `WEIGHTED` mode, just
the only mode this class offers.

### Contract

| Aspect | Rule |
|---|---|
| Strategy identity | Each strategy must expose a non-empty string `id` (callable/property `id` tolerated) and a callable `evaluate`. Duplicate ids raise `StrategyOrchestratorError`. |
| `IStrategy` | A **structural** `typing.Protocol` (`runtime_checkable`), not the SDK interface — so any duck-typed object works without importing `nexus_sdk`. `evaluate` may be sync (return a list) or async (return a coroutine); awaitable results are awaited transparently. |
| Weights | Validated up front: must be a finite number `>= 0` (`math.isfinite` gate so `NaN`/`Inf` are rejected, and a bare `w < 0` is *not* trusted on its own). |
| Shared context | Every strategy receives the **same** `market_context` object so cross-strategy comparisons are apples-to-apples. (Deep-copying inputs per strategy is `MultiStrategyPortfolio`'s job, not this class's — this one is the lightweight voter.) |
| Fault isolation | A strategy that raises is recorded in `SignalSet.errors` (`{id: "ExcType: message"}`) and the rest still vote. |
| Determinism | Resolved signals are emitted in `sorted(symbols)` order. |

<a id="signalset"></a>
### Outcome shape — `SignalSet`

A frozen dataclass, richer than a bare `list[Signal]` for the same
audit reasons the other orchestrators' results are:

| Field | Meaning |
|---|---|
| `signals` | Per-symbol resolved decision (one per considered symbol), each stamped `strategy_id="orchestrated"`. |
| `strategy_count` | Size of the registry (so an empty result vs. an empty registry are distinguishable). |
| `breakdown` | `dict[symbol, {net, side, voters:[[id, "buy"\|"sell", weight], …]}]` — full per-symbol provenance for the audit trail. |
| `errors` | `dict[id, message]` for any strategy that raised during `evaluate`. |

Convenience views: `trade_signals` (non-HOLD signals) and `is_empty`
(no symbol was considered — empty registry or no output). The constant
`ORCHESTRATED_STRATEGY_ID = "orchestrated"` is the `strategy_id` stamped
on every resolved signal so audit code can tell an orchestrator decision
apart from a raw per-strategy signal.

### When to pick it

Use this when you want the **simplest** thing that still gives you a
weighted net vote with a provenance trail and transparent sync/async
support, and you do **not** need capital allocation, per-strategy
timeouts, or majority counting. Reach for `MultiStrategyPortfolio` when
strategies compete for *capital*; reach for the async
`engine/core/strategy_orchestrator.py` when you need `MAJORITY` semantics
or per-strategy `eval_timeout` isolation.

## `engine/portfolio/multi_strategy.py` — `MultiStrategyPortfolio`

The **capital-aware** orchestrator. Where the three voters above are
pure signal voters (one signal = one vote, scaled at most by a unitless
weight), `MultiStrategyPortfolio` is the only one that knows *how much
money* each strategy may deploy. It owns three concerns the voters
deliberately do not:

1. **Capital allocation** — each strategy is registered with a
   `capital_weight` (a *relative*, non-negative share of a fixed
   `total_capital`). Weights need not sum to 1.0: dollar allocations
   (`allocation(id)`, `allocations()`) are computed on demand by
   normalising against the weight sum, so `{a:2, b:1}` deploys 2/3 to `a`
   and 1/3 to `b`. This is the source of truth for how much of the book
   a strategy can move.
2. **Evaluation** — `await evaluate_all(market_data, merge_mode=…)` runs
   every registered strategy against the *same* `market_data` and the
   portfolio's own `ICostModel` (per the cost-first spec), each strategy
   receiving an **independent `copy.deepcopy`** of both inputs so a
   misbehaving plugin cannot poison its siblings or the caller's
   originals. A single failing — or timed-out — strategy is isolated:
   its `{id: error}` lands in `errors` and the rest still contribute.
3. **Risk-adjusted signal merging** — per symbol, the **capital-weighted
   dollar exposure** is netted: signed exposure =
   `side_sign(sid) * allocation(sid) * sig.weight` (BUY `+1`, SELL `-1`,
   HOLD `0`, abstains). The merged side is the sign of the net (a
   `_NET_EPSILON` = 1e-9 dead band treats float dust as a stalemate →
   HOLD); the merged weight is `|net exposure| / total_capital` clamped
   to `[0, 1]`. This makes the merge *risk-adjusted* in two senses: a
   strategy with more capital at risk moves the decision proportionally
   more, **and** the emitted weight is itself a measure of how much of
   the book is committed. Opposing equal-dollar signals net to zero
   (stalemate → HOLD); non-finite `NaN`/`Inf` weights abstain so they
   cannot poison the sum.

The only merge mode today is `SignalMergeMode.RISK_ADJUSTED`; the enum
is reserved for future cycles (e.g. a net-position mode), mirroring the
placeholder discipline on the priority orchestrator's
`ConflictResolution`.

### Outcome shape — `PortfolioEvaluation` / `CombinedPosition`

`evaluate_all` returns a `PortfolioEvaluation`, which is richer than a
bare `list[Signal]` for the same audit/safety reasons the other
orchestrators' results are:

| Field | Meaning |
|---|---|
| `signals` | Merged decisions (HOLD-on-stalemate symbols are dropped to match the engine's "HOLD = no action" semantics). |
| `positions` | `dict[symbol, CombinedPosition]` — per-symbol net exposure, weight, and `contributors` (the strategy ids that expressed a non-HOLD opinion). A pure-HOLD symbol still appears here so risk reports know it was considered. |
| `per_strategy_signals` | Raw per-strategy provenance (`dict[id, list[Signal]]`) for the audit trail. |
| `capital_deployed` / `net_exposure` | Gross (`Σ|net exposure|`) and signed capital at risk. |
| `capital_utilization` | `capital_deployed / total_capital` — how much of the book is committed this cycle. |
| `errors` | `dict[id, message]` for any strategy that raised *or* exceeded `eval_timeout`. A timeout is reported as `TimeoutError: evaluate exceeded <N>s timeout` and never stalls the cycle. |
| `trade_signals` / `is_noop` | Convenience views: non-HOLD signals, and the "nothing to do" predicate (empty registry, all-empty results, **or** zero total capital). |

Each merged `Signal` is stamped with `strategy_id="portfolio"` and a
`metadata.portfolio_contributors` list so the decision stays auditable
end-to-end; nested `metadata` is deep-copied so downstream mutation can't
leak back into the source signals.

### Fault isolation contract

Two guards keep one bad plugin from aborting a cycle:

- **Snapshot before iterate.** `evaluate_all` snapshots the registry
  before walking it, so a strategy that registers/unregisters a sibling
  mid-cycle cannot mutate the dict under us (added strategies run next
  cycle).
- **Tight `wait_for` guard.** Only the *awaitable* result is bounded by
  the per-strategy `eval_timeout` (default 30 s); the synchronous call
  frame is not (it has already returned before the cap could apply). A
  builtin `TimeoutError` raised inside the coroutine is therefore
  classified as a strategy failure, not a deadline — only a genuine
  `asyncio.wait_for` expiry is reported as a timeout.

### When to pick it

Use `MultiStrategyPortfolio` when strategies compete for *capital* (you
have N strategies and a fixed dollar book to split between them). Use
any of the three voters above when strategies merely *vote* (one signal
per strategy, no money model). The portfolio is the heavier abstraction:
it composes a cost model and the allocation value object
([`engine/portfolio/allocation.py`](../../engine/portfolio/allocation.py))
and is the natural top-level loop once paper/live routes land.

---

<a id="drift-driven-rebalancing-portfoliorebalancer"></a>
## Drift-driven rebalancing — `PortfolioRebalancer`

`MultiStrategyPortfolio` answers *"given a capital split, what should we
trade this cycle?"* `PortfolioRebalancer`
([`engine/portfolio/rebalancer.py`](../../engine/portfolio/rebalancer.py))
answers the slower, periodic question: *"the strategies' dollar values
have drifted away from the policy weights — by how much, and what
capital transfers would restore them?"* It is the closing half of the
allocation story and lives in the same `engine/portfolio/` package
(which, by design, owns **no execution**).

Construct once with the *target* policy weights, the *current* dollar
value per strategy, and a drift `threshold` (default `0.05` = 5%); then
query three things:

| Method | Returns |
|---|---|
| `compute_drift()` | Signed `current_weight − target_weight` per strategy (positive = **overweight**, negative = **underweight**). |
| `needs_rebalance()` | `True` when `max(|drift|)` strictly exceeds `threshold`. Zero total capital is a hard `False` (nothing can move). |
| `generate_rebalance_orders()` | One `RebalanceOrder` per strategy whose current dollar value differs from target (beyond a `1e-9` float-dust floor), sorted by id for determinism. |

A `RebalanceOrder` is an **advisory signal**, not a trade: `action` is
`RebalanceAction.BUY` (underweight → add `|delta|` dollars) or `SELL`
(overweight → withdraw `|delta|`), with full provenance (`current/target
weight`, signed `drift`) so an audit trail never re-derives *why* an
order was emitted. A portfolio already on target yields an empty list.

**Design contract.** The class is **pure / no I/O** — synchronous and
effectively stateless over its construction inputs (no network, broker,
or DB call), which keeps it inside `engine.portfolio`'s "no execution"
boundary and makes it trivially unit-testable. Targets are **relative**
(normalised internally, so `{"a":1,"b":1}` ≡ `{"a":0.5,"b":0.5}`,
matching `MultiStrategyPortfolio`; an all-zero set falls back to equal
shares). Every numeric input funnels through `_finite`, which rejects
`bool` (a sneaky `int` subclass), numeric strings, `None`, and
non-finite values — `math.isfinite` is the gate because bare `w < 0`
silently admits `NaN`. Finally, `needs_rebalance` is a strict `>`
against the threshold but wraps the comparison in `math.isclose` so a
drift sitting *exactly* on the boundary is treated as within tolerance
(float noise can neither spuriously trip nor suppress a rebalance);
this edge is pinned by tests.

---

## Status

Every orchestrator and the rebalancer on this page is **library-only**
today: none is registered in the execution factory, and no public run
route drives any of them. They are all unit-tested:

| Component | Test |
|---|---|
| `engine/orchestration` `StrategyOrchestrator` | `tests/test_nexus_sdk_strategy.py` + orchestration tests |
| `engine/core/strategy_orchestrator.py` | strategy-orchestrator tests |
| `engine/portfolio/orchestrator.py` | `tests/test_portfolio_orchestrator.py` |
| `engine/portfolio/multi_strategy.py` | [`tests/test_multi_strategy_portfolio.py`](../../tests/test_multi_strategy_portfolio.py) |
| `engine/portfolio/rebalancer.py` | `tests/test_capital_allocation.py` + rebalancer tests |

The open P1 that would naturally consume all of them is the **live/paper
run route** — see [`known-limitations.md`](../known-limitations.md)
("Three Execution Modes (Roadmap: partial)" and "TaskIQ plumbing
incomplete"). Until that lands, treat these as a reviewed, tested library
surface that is one route away from being drivable.

## See also

- [`core-domains.md`](core-domains.md) — where orchestration sits in the
  domain layer (instruments → strategy → order manager → execution), and
  the cost-first `ICostModel` contract every `evaluate()` receives.
- [`ARCHITECTURE.md`](../../ARCHITECTURE.md) — the headline
  `OrderManager` → `RiskEngine` → `ExecutionBackend` pipeline that
  consumes whatever an orchestrator emits.
- [`overview.md`](overview.md) — service-level view; `engine/orchestration/`
  and `engine/portfolio/` in the top-level layout table.
