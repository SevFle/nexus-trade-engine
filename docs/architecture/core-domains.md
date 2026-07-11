# Core engine domains

[`overview.md`](overview.md) describes the engine as a *service* —
FastAPI app, request lifecycle, middleware, deploy topology. This
document is the companion view: it maps the **domain layer** under
[`engine/core/`](../../engine/core/) (and its sibling
[`engine/orchestration/`](../../engine/orchestration/)) — the modules
that turn signals into decisions, decisions into fills, and fills into
performance numbers.

The repo-root [`ARCHITECTURE.md`](../../ARCHITECTURE.md) is the
authoritative reference for the four "headline" components
(`OrderManager`, `CostModel`, `Portfolio`, `RiskEngine`,
`BacktestRunner`); we cross-reference it rather than duplicate it and
focus here on the layers that file does **not** enumerate: multi-strategy
orchestration, the analytics taxonomy, strategy governance, optimization,
and post-trade cost analysis.

## Component map

```mermaid
flowchart TD
    subgraph input["Inputs (per bar)"]
        MKT["MarketState"]
        COST["ICostModel"]
    end

    subgraph strat["Strategy layer (see multi-strategy.md)"]
        ONE["single strategy<br/>IStrategy.evaluate"]
        MANY["StrategyOrchestrator<br/>engine/orchestration"]
        PORTF["MultiStrategyPortfolio<br/>engine/portfolio<br/>(capital-aware, merges/symbol)"]
        MGR["MultiStrategyManager<br/>engine/strategies<br/>(absolute budgets, forwards all)"]
    end

    ONE --> SIG["list[Signal]"]
    MANY --> AGG["SignalAggregator<br/>majority / weighted / net"]
    AGG --> SIG
    PORTF --> SIG
    MGR --> SIG
    COST -.->|allocation| PORTF
    COST -.->|allocation cap| MGR

    SIG --> OM["OrderManager<br/>validate → cost → risk → submit"]
    COST -.->|estimate| OM
    OM --> RISK["RiskEngine<br/>(final veto)"]
    RISK --> EXEC["ExecutionBackend<br/>backtest / paper / live"]
    EXEC --> PORT["Portfolio<br/>tax-lot accounting"]
    PORT --> TAX["tax/*<br/>FIFO/LIFO, wash sale, jurisdictions"]
    EXEC --> TCA["tca.py<br/>implementation shortfall"]

    PORT --> MET["metrics.py<br/>MetricsReport (25 fields, live)"]
    MET --> EVAL["strategy_evaluator<br/>composite score [0,100]"]
    MET -.->|schema only| REPORT["analytics/*<br/>PerformanceReport (86 KPIs)"]
    MET --> MC["monte_carlo<br/>robustness"]
    MET --> OPT["param_optimizer<br/>grid / random / genetic"]

    GOV["strategy_lifecycle<br/>draft→bt→paper→live"]
    VER["strategy_versioning<br/>content-addressed deploys"]
    GOV -.->|gates promotions| ONE
    VER -.->|pinned code| ONE
```

## Instruments & multi-asset model

[`engine/core/instruments.py`](../../engine/core/instruments.py) replaces
the legacy string-`symbol` plumbing with a typed `Instrument` Pydantic
model that knows its asset class, venue, currency, and the
asset-class-specific fields (option strike/expiration, crypto base/quote,
forex pip/lot, futures multiplier). This is the engine-side identity
layer — what the OMS keys positions and lots on, distinct from the
**data-routing** taxonomy in
[`engine.data.providers.base.AssetClass`](../../engine/data/providers/base.py)
(which decides *which provider* can serve a query). The two evolve
independently; bridge them with `InstrumentAssetClass.to_provider_class()`.

`InstrumentAssetClass` is an `StrEnum` with eight members: `EQUITY`,
`ETF`, `CRYPTO`, `CRYPTO_PERP`, `CRYPTO_FUTURE`, `FOREX`, `OPTION`,
`FUTURE`. The split between spot crypto, perpetuals, and dated crypto
futures matters because they are *different products* on the same pair
— see the `uid` invariant below.

**Failure signal, not silent fallback.**
`InstrumentAssetClass.to_provider_class()` raises
`UnknownAssetClassError` for any member with no provider mapping. The
exception *is* the signal — there is no default asset class — and
constructing it also emits a `WARNING` log so an unmapped value is
visible even when the caller swallows the error (#1227). It is raised
**unconditionally, with no `__debug__` guard**, so an optimized
(`-O`) interpreter cannot silently turn a hard error into a no-op
(#1229).

Key invariants and behaviours, all enforced in the model:

| Concern | Rule |
|---|---|
| Class-specific fields | `OPTION` requires `strike`/`expiration`/`option_type`/`underlying`; `CRYPTO*` and `FOREX` require `base_asset`+`quote_asset` (else `ValueError`). |
| `uid` (stable identity) | Distinct per `(asset_class, identifying fields)`: `BTC/USD` (spot), `BTC/USD:PERP`, and `BTC/USD:<yyyymmdd>` (dated future) produce **different** uids, so positions in different products never collapse onto one key. |
| `model_copy(update=…)` | Rebuilds through `model_validate` so the symbol/whitespace validator and every class invariant run again — pydantic's default `model_copy` short-circuits validation and would let `update={"symbol":" x "}` bypass every check. |
| `from_string(raw)` | **Conservative**: defaults to `EQUITY` and treats `EUR/USD` / `BRK/B` as equity to avoid silently misclassifying slash-bearing symbols as crypto. Crypto/forex callers must use the explicit factories. |
| Legacy alias | `expiry_date` is folded into the canonical `expiration` field. |

**Integration with signals.**
[`Signal.instrument`](../../engine/core/signal.py) is a required, typed
field. For backward compatibility it is auto-populated as
`Instrument.from_string(symbol)` (i.e. equity by default) when a caller
passes only a string `symbol` — so existing backtest code keeps working.
A strategy that emits non-equity signals must construct the `Instrument`
explicitly (e.g. `Instrument.crypto("BTC", "USDT")`) so the asset class
is unambiguous.

> **Status.** The model, the per-class invariants, the provider-class
> bridge, and the market-data route's symbol-shape `detect_asset_class`
> (see [`api-reference.md`](../api-reference.md#market-data)) are landed
> and unit-tested. Multi-asset support is still **partial**: not every
> cost model and tax jurisdiction path has been validated against every
> asset class, and live/paper execution for non-equity instruments is
> not wired (see [`known-limitations.md`](../known-limitations.md)).

## Multi-strategy orchestration

Nexus has **four** cooperating classes that turn "run N strategies and
produce a tradeable signal set" into a single answer. They overlap in
spirit but are deliberately separate modules with different
conflict-resolution semantics, different responsibilities around
*capital*, and different provenance contracts:

| Class | Package | Capital model | Per-symbol merging |
|---|---|---|---|
| `StrategyOrchestrator` | `engine/orchestration` | none | yes — `PRIORITY` / `NET_POSITION` |
| `strategy_orchestrator` + `SignalAggregator` | `engine/core` | none | yes — `MAJORITY` / `WEIGHTED` |
| `MultiStrategyPortfolio` | `engine/portfolio` | relative `capital_weight` (normalised) | yes — risk-adjusted dollar-exposure netting |
| `MultiStrategyManager` *(newest, `decf8ca`)* | `engine/strategies` | absolute `allocation_pct` (sum ≤ 100) | **no** — forwards every signal, capped + re-tagged |

The one-line rule for the two capital-aware classes: the **portfolio
*merges*** into one position per symbol (relative weights); the **manager
*forwards*** every strategy's signals, just allocation-capped and
re-tagged with the caller-supplied `strategy_id` (absolute budgets,
per-strategy provenance — the contract per-strategy attribution and
per-strategy risk limits need).

The full decision matrix, outcome shapes (`PortfolioEvaluation`,
`MultiStrategyEvaluation`), validation rules, the shared fault-isolation
contract (snapshot-before-iterate + tight `wait_for` guard, HOLD-as-
abstain), and per-class status live in
[`multi-strategy.md`](multi-strategy.md). All four are **library-only**
today — none is wired to a public run route; see
[`known-limitations.md`](../known-limitations.md) for the open live/paper
P1 they would consume.

## Cost & risk modeling

The cost model is what makes Nexus "cost-first" — `ICostModel` is an
argument to every `evaluate()`, so a strategy can price in commissions,
spread, slippage, taxes, and wash-sale risk *before* emitting a signal.
The headline `DefaultCostModel` (`cost_model.py`) is documented in
[`ARCHITECTURE.md`](../../ARCHITECTURE.md). The specialized models layer on top:

| Module | Adds |
|---|---|
| [`market_impact.py`](../../engine/core/market_impact.py) | **Almgren-Chriss** square-root market-impact model (gh#96). Estimates the price drift an order *causes*, decomposed into temporary impact (reverts post-fill) and permanent impact (information leakage, does not revert). The institutional standard when order size is large vs. ADV. |
| [`tca.py`](../../engine/core/tca.py) | **Post-trade TCA.** Per-fill implementation shortfall and arrival slippage, plus `aggregate_tca()` rollups by broker and symbol. Distinct decision price (signal quote) vs. arrival price (venue entry). |
| [`execution_costs.py`](../../engine/core/execution_costs.py) / [`holding_costs.py`](../../engine/core/holding_costs.py) | Per-leg execution cost and borrow/carry holding-cost accrual. |
| [`regulatory_fees.py`](../../engine/core/regulatory_fees.py) / [`crypto_costs.py`](../../engine/core/crypto_costs.py) | Asset-class-specific statutory and venue fees. |

`RiskEngine` (`risk_engine.py`) is the **final veto** in the
`OrderManager` pipeline: it runs after the cost estimate and can reject
an order the cost model was happy with. Circuit breaker (10 % drawdown
halt), max open positions, concentration cap, single-order value cap, and
daily trade limit are the default rule set — see
[`ARCHITECTURE.md`](../../ARCHITECTURE.md) for the table.

<a id="execution-backends"></a>
## Execution backends

The order manager never calls a broker directly — it goes through the
`ExecutionBackend` ABC
([`engine/core/execution/base.py`](../../engine/core/execution/base.py)),
so the same strategy code runs unchanged in backtest, paper, and live.
The split across two packages is deliberate and worth knowing:

| Location | What it is |
|---|---|
| [`core/execution/base.py`](../../engine/core/execution/base.py) | `ExecutionBackend` ABC + `FillResult` dataclass. The single contract the order manager holds. |
| [`core/execution/backtest.py`](../../engine/core/execution/backtest.py) | `BacktestBackend` — fills at the bar's price. |
| [`core/execution/paper.py`](../../engine/core/execution/paper.py) | `PaperExecutionBackend` — simulated fills with a pluggable `SlippageModel`. |
| [`core/execution/live.py`](../../engine/core/execution/live.py) | `LiveBackend` — a **scaffold base class** (`_is_scaffold = True`). It tracks connection state but talks to *no* broker; concrete subclasses flip the flag and implement `_do_connect` / `_submit_order`. |
| [`core/execution/factory.py`](../../engine/core/execution/factory.py) | `create_backend(name)` registry. Built-ins: `backtest`, `paper`, `live`. Extensible at startup via `register_backend()`. |
| [`engine/execution/`](../../engine/execution/) *(top-level, SEV-223)* | `LiveExecutionBackend` — the **concrete** Alpaca-compatible REST adapter. Implements the ABC's `connect`/`disconnect`/`execute` *and* exposes broker-direct async helpers `submit_order` (`POST /v2/orders`), `cancel_order` (`DELETE /v2/orders/{id}`), `get_order_status` (`GET /v2/orders/{id}`). |

A second concrete Alpaca trading client lives in
[`engine/core/brokers/alpaca/`](../../engine/core/brokers/alpaca/) —
`AlpacaTradingClient` (gh#136) implements the `BrokerClient` Protocol
from [`engine/core/brokers/models.py`](../../engine/core/brokers/models.py)
and goes direct to Alpaca's REST API over an injectable `httpx.AsyncClient`
(no `alpaca-py` dependency). The two adapters are **not** unified yet:
the `brokers/` package targets the broker Protocol surface (clock,
account, positions), while `engine/execution/` targets the
`ExecutionBackend` surface the order manager calls. Pick by which
interface you hold.

Both concrete adapters share the typed error vocabulary from
[`engine/core/brokers/base.py`](../../engine/core/brokers/base.py):
`401/403 → BrokerAuthError` (permanent, kill-switch), `5xx/429/408 +
transport errors → BrokerConnectionError` (retried with backoff, then
raised), `400/404/422 → BrokerRejectError` (per-order).
`LiveExecutionBackend` also stamps a broker `client_order_id` (uuid4)
on every submit so the broker can de-duplicate retries (gh#49eec71) —
the same idempotency convention the order manager relies on.

> **Status:** the `BacktestBackend` is the only execution backend wired
> into a run path (the backtest runner). `PaperExecutionBackend`,
> `LiveBackend`, `LiveExecutionBackend`, and `AlpacaTradingClient` are
> library-only today — none is registered in the factory *and* mounted
> by a route. See [`known-limitations.md`](../known-limitations.md).

## Portfolio accounting

[`Portfolio`](../../engine/core/portfolio.py) is full tax-lot accounting.
`open_position()` deducts cash and opens a lot; `close_position()`
walks lots under the selected method (FIFO / LIFO / SPECIFIC_LOT),
computes realized P&L, and records the sale for wash-sale detection.
`snapshot()` returns an immutable `PortfolioSnapshot` handed to
strategies (mutating it can't corrupt the live book). The root
[`ARCHITECTURE.md`](../../ARCHITECTURE.md) covers the lot mechanics.

Two allocation modules sit beside it, plus the capital-aware orchestrator
documented above:

| Module | Purpose |
|---|---|
| [`capital_allocation.py`](../../engine/core/capital_allocation.py) | **Largest-remainder (Hamilton) apportionment** of total capital across strategies proportional to weights, computed in fixed-point `Decimal` so the result is exact to the cent. Floors each raw share, then distributes the leftover cents to the largest fractional remainders. |
| [`portfolio/allocation.py`](../../engine/portfolio/allocation.py) | `CapitalAllocation` — the **immutable value object** recording the split. Weights sum to exactly 1.0 (ε-tolerant), are non-negative, and the strategy count is capped by `max_strategies`. In-place mutation is blocked (gh#1042). |
| [`portfolio/multi_strategy.py`](../../engine/portfolio/multi_strategy.py) | `MultiStrategyPortfolio` — the **capital-aware runtime** that consumes an allocation, evaluates every strategy, and merges signals risk-adjusted. Full contract in [`multi-strategy.md`](multi-strategy.md). |
| [`engine/strategies/multi_manager.py`](../../engine/strategies/multi_manager.py) | `MultiStrategyManager` — the **capital-aware, provenance-preserving registry**. Absolute per-strategy `allocation_pct` budgets (sum ≤ 100), allocation-cap enforcement (scales a strategy's active weights to its fraction), and per-strategy signal provenance. Newest of the four orchestrators (`decf8ca`). Full contract in [`multi-strategy.md`](multi-strategy.md). |
| [`portfolio/rebalancer.py`](../../engine/portfolio/rebalancer.py) | `PortfolioRebalancer` — the **drift detector** that compares a portfolio's *target* policy weights against its *current* dollar allocation and emits advisory `RebalanceOrder` signals. Companion to `MultiStrategyPortfolio` (which decides *what to trade*); the rebalancer decides *how to get back to target*. See [Drift-driven rebalancing](#drift-driven-rebalancing-portfoliorebalancer) below. |

<a id="drift-driven-rebalancing-portfoliorebalancer"></a>
### Drift-driven rebalancing — `PortfolioRebalancer`

`MultiStrategyPortfolio` answers *"given a capital split, what should we
trade this cycle?"* `PortfolioRebalancer` answers the slower, periodic
question: *"the strategies' dollar values have drifted away from the
policy weights — by how much, and what capital transfers would restore
them?"* It is the closing half of the allocation story and lives in the
same `engine/portfolio/` package (which, by design, owns **no
execution**).

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

> **Status.** Like `MultiStrategyPortfolio`, the rebalancer is
> **library-only**: no route drives it and no execution layer consumes
> its `RebalanceOrder`s yet. The module imports cleanly (441 lines, no
> outstanding conflicts); the open work is the live/paper run route that
> would naturally consume it — see [`known-limitations.md`](../known-limitations.md).

Tax reporting lives in [`engine/core/tax/`](../../engine/core/tax/):
FIFO/LIFO lot matching, US wash-sale detection (`wash_sale.py`), and
per-jurisdiction summarizers exposed by `POST /api/v1/tax/report/{code}`
(`US`, `GB`, `DE`, `FR`) — see [`api-reference.md`](../api-reference.md#tax).

## Performance analytics

There are **two** analytics containers. Do not conflate them:

### `MetricsReport` — live, used today

[`engine/core/metrics.py`](../../engine/core/metrics.py). The container
the backtest runner actually computes and that `GET /backtest/results/{id}`
returns as `metrics`. ~25 scalar fields (total/annualized return, Sharpe,
Sortino, Calmar, max drawdown + duration + recovery, volatility,
win-rate, profit factor, avg winner/loser, streaks, total costs/taxes,
cost-drag %, turnover, exposure %) plus three series (`equity_curve`,
`drawdown_curve`, `rolling_metrics`).

### `PerformanceReport` — 86-KPI schema, not yet wired

[`engine/core/analytics/`](../../engine/core/analytics/) is the
**86-KPI taxonomy** (gh#97), split into eight section models:

| Section (file) | KPI range | Covers |
|---|---|---|
| `returns.py` | 1–14 | Return ratios + `PeriodReturn` value object. |
| `risk_adjusted.py` | 15–24 | Sharpe/Sortino/Omega/Calmar/Treynor/Information/Payoff/Profit-factor. Infinite/undefined ratios → `null`. |
| `drawdown.py` | 25–34 | Underwater series + duration/recovery. |
| `trades.py` | 35–50 | Win/loss rates, streaks, holding periods, cadence. |
| `costs.py` | 51–58 | Cost breakdown + slippage/IS in basis points. |
| `positions.py` | 59–66 | Exposure, long/short, concentration, simultaneous positions. |
| `volatility.py` | 67–76 | VaR/CVaR (positive magnitudes), capture ratios, tail ratio. |
| `time_analysis.py` | 77–86 | Monthly heatmap, day-of-week/hour returns, rolling Sharpe/DD, dual equity curve. |
| `report.py` | envelope | `PerformanceReport` aggregates the eight; `metric_count` asserts all 86 present. |

> **Status — accurate caveat.** The section modules and the envelope
> are on disk, but the builder (`engine/core/analytics/analyzer.py`)
> referenced by their docstrings is **not**, and nothing under
> `engine/api/` or `engine/core/backtest_runner.py` imports the report.
> So `PerformanceReport` is a landed schema, not yet a live output: the
> API surface still serves `MetricsReport`. Treat the 86-KPI contract as
> the intended shape; a future PR wires an analyzer and a route.

### Companion analytics modules

These operate on an equity curve / trade log and are independently
usable:

| Module | Computes |
|---|---|
| [`rolling_metrics.py`](../../engine/core/rolling_metrics.py) / [`rolling_correlation.py`](../../engine/core/rolling_correlation.py) / [`rolling_benchmark.py`](../../engine/core/rolling_benchmark.py) / [`rolling_trade_stats.py`](../../engine/core/rolling_trade_stats.py) | Rolling-window Sharpe, correlation, benchmark-relative, and trade statistics. |
| [`drawdown_analytics.py`](../../engine/core/drawdown_analytics.py) | Drawdown depth/duration/recovery + excursion stats ([`excursion_stats.py`](../../engine/core/excursion_stats.py)). |
| [`distribution_metrics.py`](../../engine/core/distribution_metrics.py) | VaR / CVaR / skew / kurtosis / tail ratio. |
| [`benchmark_comparison.py`](../../engine/core/benchmark_comparison.py) | Alpha/beta/R-squared, up/down capture, tracking error vs. a benchmark. |
| [`cumulative_returns.py`](../../engine/core/cumulative_returns.py) | Cumulative + `PeriodReturn` series (daily/weekly/monthly). |
| [`monte_carlo.py`](../../engine/core/monte_carlo.py) | Robustness testing — `bootstrap_returns` (Efron i.i.d.) and `block_bootstrap` (Künsch, preserves autocorrelation). Pure numpy, deterministic given a `seed`. |
| [`portfolio_aggregator.py`](../../engine/core/portfolio_aggregator.py) / [`portfolio_concentration.py`](../../engine/core/portfolio_concentration.py) | Cross-portfolio rollups and concentration metrics. |

## Strategy scoring & governance

### Composite scoring — [`strategy_evaluator.py`](../../engine/core/strategy_evaluator.py)

Turns a `MetricsReport` into one number in `[0, 100]` plus a
per-dimension breakdown, a letter grade, and warnings. Used by the
marketplace ranking, A/B comparison surfaces, and the backtest summary
endpoint (so the UI doesn't re-derive it). **Six dimensions**, each
normalized to `[0, 100]`:

| Dimension | Inputs |
|---|---|
| `RISK_ADJUSTED_RETURN` | Sharpe ratio, piecewise mapping. |
| `DRAWDOWN_CONTROL` | Max drawdown, piecewise mapping. |
| `CONSISTENCY` | Coefficient of variation of rolling-window Sharpe. |
| `COST_EFFICIENCY` | Exponential decay on `cost_drag_pct`. |
| `WIN_RATE_QUALITY` | `win_rate × (avg_winner / |avg_loser|)`. |
| `STABILITY` | Annual volatility, piecewise mapping. |

Default weights mirror the spec and sum to 1.0 (risk-adjusted 0.30, …).
Composite = `Σ(dimension × weight)`. The evaluator is **stateless** —
construct one per weight configuration. (See also the persisted
`scoring_snapshots` table, written by the scoring routes.)

### Lifecycle & versioning

Two services govern *when* a strategy may run and *what code* runs:

| Module | Responsibility |
|---|---|
| [`strategy_lifecycle.py`](../../engine/core/strategy_lifecycle.py) | State machine `draft → backtest → paper → live`, with `retired` reachable from any non-draft stage. **No skipping** — you cannot jump draft→live. Each promotion is gated by a `LifecycleEvidence` payload: paper requires a backtest id + minimum Sharpe; live requires a paper window + minimum live-paper Sharpe. |
| [`strategy_versioning.py`](../../engine/core/strategy_versioning.py) | **Content-addressed deploys.** A `StrategyVersion` is a SHA-256 of the code blob + a hash of its canonical-JSON config, so the same code+config never creates two records (re-deploys are idempotent). `deploy` → new `DRAFT`; `activate` promotes to `ACTIVE` (demoting the prior); `rollback` returns to a previous `ACTIVE`. |

Together: `StrategyVersionService` controls *what* code runs;
`StrategyLifecycleService` controls *which stage* it's allowed to run at.

## Optimization — [`param_optimizer.py`](../../engine/core/param_optimizer.py)

Pure-Python hyperparameter search against an objective function
(typically a backtest's Sharpe or compound return).

- **`ParameterSpace`** — typed search space (continuous / discrete /
  categorical dimensions).
- **`Optimizer`** — Protocol the algorithms implement.
- **`optimize(...)`** — the dispatch entry point.

Algorithms shipped (PR1): `GridSearchOptimizer`,
`RandomSearchOptimizer`, `GeneticOptimizer`. Bayesian search and
Hyperband are TODO and noted in the module docstring. Runs are
deterministic given a seed; pair with `monte_carlo` to sanity-check that
an "optimal" parameter set isn't an overfit artifact.

## Wired vs. schema-only (quick reference)

| Capability | Wired into a route/runner? |
|---|---|
| `MetricsReport`, `strategy_evaluator`, `monte_carlo`, `param_optimizer` | Yes — consumed by the backtest runner / scoring routes. |
| `MultiStrategyPortfolio`, `MultiStrategyManager` | **No** — both capital-aware orchestrators are library-only and fully unit-tested today; the live/paper run route that would drive them is the open P1 (see [`multi-strategy.md`](multi-strategy.md) and [`known-limitations.md`](../known-limitations.md)). |
| `tca.py`, `market_impact.py` | Library-only today — no public route, and neither is consumed by `DefaultCostModel` yet (the square-root model is available for strategies / evaluators to call directly). |
| `PerformanceReport` (86-KPI) | **No** — schema landed, no analyzer/route yet. |
| `strategy_lifecycle` / `strategy_versioning` | Library-only; the public promotion/version API is part of the still-partial live-trading story (see [`known-limitations.md`](../known-limitations.md)). |

## See also

- [`ARCHITECTURE.md`](../../ARCHITECTURE.md) — headline components
  (`OrderManager`, `CostModel`, `Portfolio`, `RiskEngine`,
  `BacktestRunner`), the signal→fill pipeline, and the SDK plugin
  interface.
- [`overview.md`](overview.md) — service-level view (app factory,
  middleware, request lifecycle, deploy topology).
- [`plugins.md`](plugins.md) — strategy discovery, the registry, and the
  five-layer sandbox.
- [`known-limitations.md`](../known-limitations.md) — what is
  half-built, including the live/paper execution and TaskIQ wiring.
