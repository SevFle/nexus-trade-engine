# ADR-0010: Multi-strategy orchestration — two orchestrators and HOLD-as-side

- **Status:** Accepted
- **Date:** 2026-07-01
- **Supersedes:** —
- **Related:** [`architecture/orchestration.md`](../architecture/orchestration.md),
  ADR-0001 (scaffold tech choices)

## Context

Strategies in Nexus emit `Signal` objects — that contract is fixed (see
[`engine/core/signal.py`](../../engine/core/signal.py)). The moment more
than one strategy runs against the same book, the engine must answer two
questions for every symbol on every cycle:

1. *Evaluation* — how do N strategies see identical inputs without one
   bad plugin poisoning the rest, and how do we keep one timeout from
   stalling the whole cycle?
2. *Conflict resolution* — two strategies emit opposing signals on the
   same symbol. Who wins?

The codebase landed **two** components that answer those questions, in
that order:

1. `engine/core/signal_aggregator.py` — pure per-symbol voting
   (`SignalAggregator`), plus `engine/core/strategy_orchestrator.py` —
   an async orchestrator that owns a weighted registry, deep-copies
   inputs per strategy, enforces a per-strategy timeout, and delegates
   voting to the aggregator.
2. `engine/orchestration/orchestrator.py` — a second `StrategyOrchestrator`
   (same class name, different package), self-contained, with a new
   `NET_POSITION` conflict model.

A reviewer's first reaction to "two classes named
`StrategyOrchestrator`" is rightly *"why aren't these one class?"*
This ADR records the answer so the question stops recurring in review.

## Decision

We keep **both** orchestrators and accept the duplicated name. The two
serve different operating points, and collapsing them would either
re-add a dependency the light one deliberately dropped or strip
isolation guarantees the async one must keep.

### 1. `HOLD` is a first-class `Side`, not the absence of a signal

A strategy that has *looked at* a symbol and decided to do nothing is
information the aggregator wants:

- Under `UNANIMOUS`, a `HOLD` counts as agreement (silence ≠ veto).
- Under `MAJORITY` / `WEIGHTED`, a `HOLD` abstains — it never enters the
  denominator, so 1 BUY + 1 HOLD is a clean BUY, not a 1-1 tie.

If "no opinion" were modelled as "omit the symbol", the aggregator
could not distinguish *considered and declined* from *never evaluated*,
and `UNANIMOUS` semantics would be ambiguous. Making `HOLD` a value
rather than an absence is what makes every voting rule well-defined.

### 2. Conflict resolution is a taxonomy, not a single algorithm

We standardise five resolution rules across the codebase, each with a
non-overlapping use case:

| Rule | Models "…" wins | Lives in |
|---|---|---|
| `INDEPENDENT` | (no resolution — disjoint universes) | `SignalAggregator` |
| `UNANIMOUS` | everyone who had an opinion | `SignalAggregator` |
| `MAJORITY` | a strict headcount majority | `SignalAggregator` |
| `WEIGHTED` | the higher conviction-weighted side | `SignalAggregator` |
| `PRIORITY` | the single highest-priority strategy | both |
| `NET_POSITION` | conviction-weighted net exposure | light orchestrator only |

All ties resolve to `HOLD`, never to an arbitrary winner — relying on
dict iteration order for a tie-break would be non-deterministic across
Python versions. `NET_POSITION` is the one rule *voting* cannot express:
it treats `weight` as conviction, so two half-weight BUYs outvote one
full-weight SELL. Under majority/weighted voting each strategy is a
single vote regardless of weight magnitude. That distinction is
load-bearing for net-exposure portfolio construction, which is why
`NET_POSITION` exists at all.

### 3. Voting math has one home

`SignalAggregator` is the single source of truth for tie handling. The
async orchestrator builds one per `evaluate_all()` call and never
reimplements votes. The light orchestrator is the deliberate exception:
it implements `PRIORITY` and `NET_POSITION` inline because it wanted to
ship without the aggregator dependency and because `NET_POSITION` isn't
in the aggregator. If `NET_POSITION` later moves into the aggregator,
the light orchestrator should delegate and drop its inline math.

### 4. The two orchestrators serve different operating points

| | async (`engine.core`) | light (`engine.orchestration`) |
|---|---|---|
| Built for | untrusted / third-party plugins | controlled, in-house strategies |
| Input isolation | `copy.deepcopy` per strategy | shared reference |
| Timeout isolation | per-strategy `asyncio.wait_for` | none |
| Provenance | rich `OrchestrationResult` (batches + errors) | `list[Signal]` |
| Extra conflict model | — | `NET_POSITION` |

Collapsing them into one class would force every backtest fan-out to pay
for deep-copy + timeout machinery it doesn't need, *or* strip those
guarantees from the path that runs untrusted code. Neither trade-off is
worth the name deduplication.

## Consequences

- **Positive**
  - The contract (`Signal` → `SignalAggregator` → orchestrator) is
    layered; each tier is independently testable.
  - Conflict resolution is a discoverable taxonomy instead of ad-hoc
    per-feature logic.
  - Backtest fan-out can use the light orchestrator's `NET_POSITION`
    without importing the heavier async machinery.
- **Negative**
  - Two public classes named `StrategyOrchestrator`. Callers must import
    from the right package; IDE auto-import will occasionally pick the
    wrong one. We accept this in exchange for the operating-point
    separation and mitigate it with the selection guide in
    [`orchestration.md`](../architecture/orchestration.md#which-orchestrator-do-i-use).
  - `NET_POSITION` logic is duplicated-by-design in the light
    orchestrator. If it ever lands in `SignalAggregator`, the light
    orchestrator's inline copy should be removed in the same change.
  - Neither orchestrator is wired to an HTTP route yet, so the
    duplication is invisible to API consumers. Binding a route is the
    natural moment to revisit whether the public surface should expose
    one name or two.
- **Neutral**
  - The light orchestrator ships without tests (commit `dcd9483`).
    `tests/test_orchestration.py` is a prerequisite before any route
    binds it; tracked in
    [`known-limitations.md`](../known-limitations.md).

## Alternatives considered

- **One orchestrator, mode flag for isolation.** Rejected: isolation
  (deep-copy, timeout) is not a *mode*, it's a guarantee. A flag that
  turns off deep-copy for performance invites "forgot to enable it in
  production" bugs on the path that runs untrusted code.
- **`HOLD` as omission.** Rejected: makes `UNANIMOUS` ambiguous and
  loses the "considered and declined" signal (see §1).
- **Put `NET_POSITION` in `SignalAggregator` now.** Deferred: the
  aggregator's contract is "one signal per `(strategy, symbol)` per
  batch, side ∈ {BUY, SELL, HOLD}". `NET_POSITION` adds
  magnitude-as-vote semantics that would complicate the other four
  modes' invariants. Revisit when a route needs both `NET_POSITION` and
  the async orchestrator's isolation together.
