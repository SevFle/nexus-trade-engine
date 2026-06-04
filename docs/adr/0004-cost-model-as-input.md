# ADR-0004: Cost model is a strategy input, not a post-hoc adjustment

- **Status**: Accepted
- **Date**: 2026-04-22
- **Deciders**: lead maintainer + strategy author
- **Tags**: core, strategies, cost-model

## Context and Problem Statement

Most retail backtest frameworks apply transaction costs as a post-
processing step: the strategy emits idealized signals, the engine
subtracts a fixed commission per trade, and the resulting equity
curve is what gets reported. This systematically misleads strategy
authors in three concrete ways we have hit on prior projects:

1. **Wash-sale and tax-lot decisions are invisible to the strategy.**
   A strategy that "sells at a loss" looks identical to one that
   sells at a gain, even though the after-tax economics are wildly
   different under FIFO/LIFO/specific-identification regimes.
2. **Spread and slippage are not symmetric across symbols.** A
   small-cap strategy that ignores per-symbol liquidity will
   backtest at full size and trade at half size in production.
3. **Holding costs (FX carry, borrow, margin interest) compound
   silently.** Strategies that hold leveraged ETFs for months look
   200–600 bps/yr better in a backtest that ignores borrow.

The Nexus engine is explicitly aimed at strategies that survive the
transition from backtest to live. That requires the strategy itself
to see the same cost information the execution layer will pay.

## Decision Drivers

- **Fidelity across modes.** A backtest, paper trade, and live trade
  must produce identical signals when fed identical market data. If
  costs are applied differently in each mode the contract breaks.
- **Strategy author freedom.** The engine should not prescribe how a
  strategy weighs cost vs. alpha. A market-maker and a long-only
  retirement fund have totally different cost sensitivities.
- **Auditable tax lots.** Regulators (and users) need to be able to
  reconstruct "why did the strategy sell here" months later. If cost
  is opaque, that audit trail is incomplete.

## Considered Options

1. **Cost-as-input** — `evaluate(portfolio, market, costs)`; the
   strategy reads `CostBreakdown` and decides.
2. **Cost-as-postprocess** — strategy returns idealized signals;
   engine subtracts a `CostModel` afterwards.
3. **Cost-as-middleware** — strategy returns signals; a middleware
   layer rewrites them (e.g. collapses a buy+sell into a no-op if
   the spread exceeds the expected gain).

## Decision Outcome

Chosen option: **Option 1 — cost-as-input**, because it preserves
strategy author freedom while making the cost information first-
class. The other two options force a single cost-handling policy on
every strategy, which is the exact failure mode this engine is
designed to avoid.

### Consequences

- **Positive** — strategies that respect costs in `evaluate()`
  transfer to live trading with no signal drift. The
  `cost_drag_pct` metric on every backtest result gives authors a
  direct measure of how much their signals are being eroded.
- **Positive** — tax lots, wash-sale disallowed losses, and
  withholding tax are visible to the strategy at decision time. The
  strategy can choose to realise a loss for the tax benefit, or
  avoid a wash sale.
- **Negative** — strategies that ignore `costs` in their first
  version look unrealistically profitable in backtest. This is a
  documentation problem, not an architecture problem: the
  `PLUGIN_DEV_GUIDE.md` calls it out and the scoring rubric in
  `engine/core/strategy_evaluator.py` penalises strategies whose
  backtest cost-drag diverges from the live cost-drag estimate.
- **Neutral** — the `CostBreakdown` schema is part of the SDK's
  public surface (`sdk/nexus_sdk/types.py`). Adding a new cost
  dimension is a backward-compatible addition; removing one is a
  breaking change to the SDK.

## Pros and Cons of the Options

### Option 1 — cost-as-input (chosen)

- **Pros:** matches the three drivers above; small surface area
  (one extra parameter); makes the cost visible at every layer of
  the call stack instead of hidden in the executor.
- **Cons:** the strategy author has to learn what each cost field
  means. The contract is also more brittle: a new cost dimension
  requires a coordinated SDK + engine release.

### Option 2 — cost-as-postprocess

- **Pros:** simpler contract for strategy authors (3 fields instead
  of 4); engine retains total control over cost application.
- **Cons:** cannot model tax-lot-aware strategies; cannot model
  liquidity-sensitive sizing; cannot model holding-cost-aware
  rebalancing. Defeats the purpose of the cost model.

### Option 3 — cost-as-middleware

- **Pros:** preserves a clean signal schema; middleware is hot-
  swappable.
- **Cons:** middleware ordering becomes a correctness concern; tax-
  lot decisions still need to be visible to the strategy; harder to
  audit six months later because the signal the user sees does not
  match what got executed.

## Links

- Implementation: [`engine/core/cost_model.py`](../../engine/core/cost_model.py),
  [`sdk/nexus_sdk/types.py`](../../sdk/nexus_sdk/types.py).
- Wash sale: [`engine/core/tax/wash_sale.py`](../../engine/core/tax/wash_sale.py).
- Cost-drag metric: [`engine/core/strategy_evaluator.py`](../../engine/core/strategy_evaluator.py).
- Related: ADR-0001 (scaffold tech choices).
