<!--
Part of the MkDocs Material site (see ../README.md for the doc-stack
rationale). This page documents the multi-strategy coordination layer —
the modules that sit above individual IStrategy plugins and decide how
their signals combine. It is the deep-dive companion to the
"Multi-strategy orchestration" section of core-domains.md.
-->

# Multi-strategy coordination

Strategies emit `Signal`s; **coordinators** decide what happens when
several strategies run together. Nexus has *five* coordinators spread
across three packages. They overlap in spirit but are deliberately
separate, because they answer different questions and make different
trade-offs about capital, provenance, and conflict resolution. Picking
the wrong one is a real bug class, so this page is the authoritative
map.

> This page is the deep-dive. For the one-paragraph summary and the
> cross-references into the rest of the domain layer, see
> [`core-domains.md`](core-domains.md#multi-strategy-orchestration).

## Why more than one

The temptation is to have a single `combine(signals)` function. We don't,
because the answers to three independent questions change the right
design:

1. **Does a strategy own money, or just a vote?** A pure voter treats one
   signal as one opinion (optionally scaled by a unitless weight). A
   *capital-aware* coordinator knows how many dollars each strategy may
   move, which is the only thing that makes "risk-adjusted" or
   "budget-capped" merges meaningful.
2. **Do conflicting symbols collapse to one decision, or stay separate?**
   A *merging* coordinator nets BUY vs. SELL on the same symbol into one
   `Signal` (and a stalemate becomes HOLD). A *forwarding* coordinator
   keeps every signal distinct so downstream attribution and per-strategy
   risk limits can see who said what.
3. **Whose `strategy_id` wins?** Some coordinators re-tag every emitted
   signal with the caller's registered id (so a misbehaving plugin can
   never impersonate a sibling). Others stamp a single sentinel
   (`"orchestrated"`, `"portfolio"`) on the merged output.

The five coordinators below are the five useful combinations of those
answers.

## The map

| Coordinator | Package | Capital-aware? | Symbol conflict | Provenance | Status |
|---|---|---|---|---|---|
| `StrategyOrchestrator` | [`orchestration/orchestrator.py`](../../engine/orchestration/orchestrator.py) | no | **merges** (priority / net-position) | sentinel | library-only |
| `StrategyOrchestrator` (async) | [`core/strategy_orchestrator.py`](../../engine/core/strategy_orchestrator.py) | no | **merges** (majority / weighted, via `SignalAggregator`) | sentinel | library-only |
| `StrategyOrchestrator` (lightweight) | [`portfolio/orchestrator.py`](../../engine/portfolio/orchestrator.py) | no | **merges** (net weighted vote) | sentinel (`"orchestrated"`) | library-only |
| `MultiStrategyPortfolio` | [`portfolio/multi_strategy.py`](../../engine/portfolio/multi_strategy.py) | **yes** (relative share) | **merges** (risk-adjusted dollar netting) | sentinel (`"portfolio"`) + contributors list | library-only |
| `MultiStrategyManager` | [`strategies/multi_manager.py`](../../engine/strategies/multi_manager.py) | **yes** (absolute budget) | **forwards** (no merge) | **re-tags with caller id** | library-only |

> **Three classes are named `StrategyOrchestrator`.** They live in
> different packages and have different conflict-resolution semantics
> (priority/net-position, majority/weighted, and net-weighted-vote
> respectively). Always qualify by import path; the bare class name is
> ambiguous. The `engine.strategies` package docstring frames
> `MultiStrategyManager` as the layer that binds strategies to *capital*,
> sitting above the voters.

All five are **library-only today**: none is driven by a public run route
or registered in the execution factory. They are all fully unit-tested
(`tests/test_multi_strategy_manager.py`, `tests/test_multi_strategy_portfolio.py`,
and the orchestration tests). The live/paper run route that would
naturally consume them is the open P1 — see
[`known-limitations.md`](../known-limitations.md).

## The voters (capital-free)

These three answer *"given N opinions, what is the consensus?"* No money
model — a strategy's influence is at most a unitless weight.

### `orchestration/orchestrator.py` — two-step priority / net-position

The "register N, run all, then collapse" loop. Two-step API:
`await orch.run_all(market)` collects every strategy's signals, then
`aggregate_signals()` resolves conflicts. `ConflictResolution` selects
the merge rule:

| Mode | Rule |
|---|---|
| `PRIORITY` *(default)* | Highest-priority strategy with a non-HOLD opinion wins. Opposing signals from strategies tied at top priority → HOLD (stalemate). HOLD abstains. |
| `NET_POSITION` | `BUY = +weight`, `SELL = −weight` summed per symbol. Positive net → BUY, negative → SELL, zero → HOLD. Resolved weight is the net magnitude clamped to `[0, 1]`, so conviction can override headcount. *(Unique to this orchestrator.)* |

### `core/strategy_orchestrator.py` — async majority / weighted

The heavier async counterpart. Each strategy is registered with a
per-strategy `weight`; every strategy sees the *same* `market_data` and
`cost_model` so cross-strategy comparisons are apples-to-apples; a single
failing strategy is isolated. Dispatches to
[`SignalAggregator`](../../engine/core/signal_aggregator.py), the single
source of truth for tie handling:

| Mode | Rule |
|---|---|
| `MAJORITY` | Strictly more than half of BUY-vs-SELL votes wins; tie → HOLD. HOLD abstains and is excluded from the denominator. |
| `WEIGHTED` | Vote × registered weight (default 1.0); strictly higher total wins; tie → HOLD. Lets a high-conviction strategy override a numerical majority. |

### `portfolio/orchestrator.py` — lightweight net-weighted-vote

The smallest of the three voters, and the one most often overlooked
because it shares a package with `MultiStrategyPortfolio`. A synchronous
register-once / `evaluate(market_context)` loop that aggregates signals
into a [`SignalSet`](../../engine/portfolio/orchestrator.py) by **net
weighted vote**: each BUY casts `+weight`, each SELL casts `−weight`, HOLD
abstains; the side with the strictly greater net weight wins, and an
exact tie resolves to HOLD ("strongest net weight wins"). Resolved
signals are stamped with the `ORCHESTRATED_STRATEGY_ID` sentinel so audit
code can tell a merged decision apart from a raw per-strategy signal.

Pick it when you want the weighted-vote semantics of the async
orchestrator without the timeout/async machinery — it is the natural
drop-in for a synchronous backtest loop.

## The capital-aware coordinators

These two answer *"given a fixed dollar book, how much may each strategy
deploy, and what is the net position?"* They are the two halves of the
allocation story, and they make **opposite** choices about merging.

### `portfolio/multi_strategy.py` — `MultiStrategyPortfolio` (merge)

The merging, risk-adjusted coordinator. Each strategy is registered with
a `capital_weight` — a **relative** share (need not sum to 1.0; dollar
allocations are normalised on demand). Per symbol, the capital-weighted
**dollar exposure** is netted:

```
signed_exposure = side_sign(strategy_id) * allocation(strategy_id) * signal.weight
                     # BUY +1, SELL −1, HOLD 0
merged.side   = sign( Σ signed_exposure )        # |net| < ε → HOLD
merged.weight = | Σ signed_exposure | / total_capital   # clamped to [0,1]
```

The merge is *risk-adjusted* in two senses: a strategy with more capital
at risk moves the decision proportionally more, **and** the emitted
weight is itself a measure of how much of the book is committed.
Opposing equal-dollar signals net to zero (stalemate → HOLD).

Outcome is a `PortfolioEvaluation` richer than a bare `list[Signal]`:
merged `signals`, per-symbol `CombinedPosition`s (with `contributors`),
full `per_strategy_signals` provenance, `capital_deployed` /
`net_exposure` / `capital_utilization`, and an `errors` map. The only
merge mode today is `SignalMergeMode.RISK_ADJUSTED` (the enum is reserved
for future cycles). See [`core-domains.md`](core-domains.md) for the full
field-by-field contract.

### `strategies/multi_manager.py` — `MultiStrategyManager` (forward)

The forwarding, provenance-preserving coordinator — and the natural
choice when you need **per-strategy attribution and per-strategy risk
limits**, because it refuses to collapse disagreeing signals into one.

```python
from engine.strategies import MultiStrategyManager

manager = MultiStrategyManager(total_capital=100_000.0, eval_timeout=30.0)
manager.register("momentum",   momentum_strategy,  allocation_pct=60)
manager.register("mean-revert", mean_revert_strategy, allocation_pct=30)
# 10% of the book left unallocated by design.

result = await manager.evaluate_all(market_data, cost_model)
result.signals                  # every emitted signal, capped + re-tagged
result.per_strategy_signals     # {"momentum": [...], "mean-revert": [...]}
result.allocation_caps          # {"momentum": 60000.0, "mean-revert": 30000.0}
result.allocation_adjustments   # {"momentum": 0.83}  ← scaled down
result.errors                   # {} unless a strategy failed / timed out
```

It owns four concerns the voters and `MultiStrategyPortfolio` do not:

1. **Registration with explicit ids.** The `strategy_id` is a
   caller-supplied parameter, *not* derived from `strategy.id`. It is the
   source of truth for provenance: every emitted signal is re-tagged with
   the registered id, so a strategy that mislabels its own
   `Signal.strategy_id` can never impersonate a sibling or hide its
   origin.
2. **Per-strategy capital budgets.** Each registration carries a dollar
   cap = `total_capital * allocation_pct / 100`. `allocation_pct` is an
   **absolute** percentage in `[0, 100]`, and the sum across the registry
   must not exceed 100 (registering a strategy that would push the total
   over raises `MultiStrategyManagerError`). `0` is allowed — it registers
   a strategy that is intentionally capital-starved (paused) while
   keeping its code path warm.
3. **Allocation-cap enforcement.** If a strategy's active (BUY/SELL)
   signal weights collectively exceed its allocation fraction
   (`allocation_pct / 100`), *that strategy's* weights are scaled down
   proportionally so the cap is respected exactly. HOLD signals abstain
   and pass through unchanged. The original weights are preserved in each
   signal's `metadata` (`allocation_original_weight`,
   `allocation_capped`), so the adjustment is auditable rather than
   silent. A `pct=0` strategy has its active weights zeroed (intent
   preserved, no capital deployed).
4. **Provenance-preserving aggregation.** Unlike
   `MultiStrategyPortfolio`, the manager does **not** merge conflicting
   symbols — it forwards every signal, just allocation-capped and
   re-tagged. Downstream consumers see exactly who said what. Pair it
   with a voter (e.g. `portfolio/orchestrator.py`) if you *also* need
   symbol-level conflict resolution.

#### `MultiStrategyEvaluation` outcome

| Field | Meaning |
|---|---|
| `signals` | All emitted signals across every strategy (capped, re-tagged). |
| `per_strategy_signals` | `dict[id, list[Signal]]` — full per-strategy provenance. |
| `total_capital` | The manager's dollar budget (echoed for consumers). |
| `allocation_caps` | `dict[id, dollars]` — the per-strategy budget actually applied. |
| `allocation_adjustments` | `dict[id, scale]` for each strategy whose weights were scaled (`0 < scale < 1`; `0.0` for a capital-starved strategy). Absent if no adjustment. |
| `errors` | `dict[id, message]` for any strategy that raised *or* exceeded `eval_timeout`. A timeout is reported as `TimeoutError: evaluate exceeded <N>s timeout`. |

Convenience properties: `trade_signals` (non-HOLD), `is_noop` (empty
registry, all-empty results, or all-failed), `strategy_count`.

#### Fault isolation & input safety

Two guards keep one bad plugin from aborting a cycle, mirroring
`MultiStrategyPortfolio`:

- **Independent deep copies.** Each strategy receives a fresh
  `copy.deepcopy` of `market_data` and `cost_model`, so a misbehaving
  plugin cannot poison its siblings or the caller's originals.
- **Tight `wait_for` guard.** Only the *awaitable* result is bounded by
  `eval_timeout`; the synchronous call frame is not (it has already
  returned). A builtin `TimeoutError` raised inside the coroutine is
  therefore classified as a strategy failure, not a deadline — only a
  genuine `asyncio.wait_for` expiry is reported as a timeout. Sync
  strategies (returning a `list`) are supported transparently and are
  never subject to the timeout.
- **Snapshot before iterate.** `evaluate_all` snapshots the registry
  before walking it, so a strategy that registers/unregisters a sibling
  mid-cycle cannot mutate the dict under us (added strategies run next
  cycle).

#### Construction hardening (recent)

`MultiStrategyManager.__init__` validates every numeric parameter through
a single `_finite` gate, which rejects `bool` (a sneaky `int` subclass),
numeric strings, `None`, and non-finite values — `math.isfinite` is the
check because a bare `w < 0` silently admits `NaN`. Two recent fixes
closed edge cases in that gate:

- **#1390** — `max_strategies` is validated via `float(max_n).is_integer()`
  rather than `max_n.is_integer()`, so the integrality check also works
  for plain `int` inputs on Python < 3.12 (where `int` has no
  `is_integer` method). The integrality check runs *before* the range
  check so a fractional input yields a clear "must be an integer" error
  rather than a misleading truncation complaint.
- **#1392** — the `_finite` coercion catches `OverflowError` (e.g.
  `float(10 ** 400)`) and reports it as non-finite, instead of letting
  it abort construction with an unguarded `OverflowError`.

These are the only coordinator whose construction validates its inputs
this strictly; the others rely on the caller passing sane types.

#### When to pick it over `MultiStrategyPortfolio`

| You want… | Pick |
|---|---|
| One merged decision per symbol, with conviction scaled by capital at risk | `MultiStrategyPortfolio` |
| Per-strategy attribution preserved end-to-end, with each strategy capped to its own dollar budget | `MultiStrategyManager` |
| A relative capital split (weights need not sum to 1.0) | `MultiStrategyPortfolio` |
| An absolute capital split (percentages must sum to ≤ 100) | `MultiStrategyManager` |

The two are **not** interchangeable. `MultiStrategyPortfolio` is the
right top-level loop when strategies compete for a shared book and you
want a single netted position per symbol. `MultiStrategyManager` is the
right loop when strategies run with independent budgets and you must
attribute every signal to its source — the contract multi-strategy
attribution reports and per-strategy risk limits need. It composes
cleanly with a downstream voter if you later need symbol-level conflict
resolution too.

## Sibling: drift-driven rebalancing

`MultiStrategyPortfolio` and `MultiStrategyManager` both answer *"what
should we trade this cycle?"* [`PortfolioRebalancer`](../../engine/portfolio/rebalancer.py)
answers the slower, periodic question: *"the strategies' dollar values
have drifted away from the policy weights — by how much, and what
capital transfers would restore them?"* It emits advisory
`RebalanceOrder`s (BUY underweight / SELL overweight), is pure / no-I/O,
and lives in the same `engine/portfolio/` package (which owns no
execution by design). See [`core-domains.md`](core-domains.md) for its
full contract; it is the closing half of the allocation story and is
also library-only today.

## See also

- [`core-domains.md`](core-domains.md) — the domain-layer map this page
  extends, including the cost/risk and analytics layers.
- [`known-limitations.md`](../known-limitations.md) — the live/paper run
  route that would drive these coordinators is the open P1.
- [`adr/`](../adr/README.md) — no dedicated ADR yet for the multi-coordinator
  split; capture one (from [`../adr/template.md`](../adr/template.md)) the next
  time a sixth coordinator is proposed, so the "why five" rationale
  doesn't live only in this page.
