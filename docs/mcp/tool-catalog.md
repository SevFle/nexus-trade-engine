# MCP tool catalog

The authoritative reference for every tool the Nexus MCP server
advertises via `tools/list`. This is the human-readable companion to the
machine-readable [`api-surface-map.yaml`](api-surface-map.yaml), which is
**generated** from [`engine/mcp/tool_definitions.py`](../../engine/mcp/tool_definitions.py)
— when in doubt, the code is the source of truth.

For the design behind these tools (why read-only, how auth works, how
results are bounded), read [MCP server](../mcp-server.md). For the
whole-surface audit, read the [capability audit](capability-audit.md).

## Conventions

- **Every tool** advertises MCP `readOnlyHint=true, destructiveHint=false,
  idempotentHint=true`. There is no tool that places live orders or
  mutates trading state.
- **Default role** is `viewer`; only `run_backtest` requires `quant_dev`.
  Roles follow the shared REST hierarchy
  ([api-reference.md §roles](../api-reference.md#roles-rbac-hierarchy)).
- **Pagination** (`limit` + opaque `cursor`) applies to the three
  list-heavy tools (`get_orders`, `list_strategies`, `get_market_data`).
  Defaults: `default_page_size=50`, `max_page_size=500`.
- **Portfolio tools** take an optional `portfolio_id` (default `"default"`)
  and are gated by the ownership model in
  [MCP server §portfolio-access-control](../mcp-server.md#portfolio-access-control).

---

## Compute tools

### `run_backtest`

> Required role: **`quant_dev`** · compute-only · never places live orders

Run a historical backtest of a strategy on a single symbol over a date
range. Returns performance metrics (total return, Sharpe, max drawdown,
win rate, cost drag), trade count, and final capital.

| Property | Type | Required | Notes |
|---|---|---|---|
| `strategy_name` | string | ✅ | Installed strategy (see `list_strategies`). |
| `symbol` | string | ✅ | 1–16 chars. |
| `start_date` | date | ✅ | ISO-8601, inclusive. |
| `end_date` | date | ✅ | ISO-8601, inclusive. |
| `initial_capital` | number | — | Default `100000`, must be `> 0`. |

The result is a deliberate **summary** — never the full equity curve or
trade log (those would overrun the assistant's context). See
[MCP server §backtest-result-shape](../mcp-server.md#backtest-result-shape).

---

## Portfolio tools

All five take an optional `portfolio_id` (default `"default"`).

### `get_portfolio_status`

Cash, total market value, total return %, realized P&L, open-position
count.

### `get_positions`

Open positions: qty, avg cost, price, market value, cost basis,
unrealized P&L (abs + %), weight.

### `get_position`

One position by symbol (case-insensitive match). Raises a not-found error
when the symbol is not held.

| Property | Type | Required | Notes |
|---|---|---|---|
| `symbol` | string | ✅ | 1–16 chars, case-insensitive. |

### `get_unrealized_pnl`

Net open (unrealized) P&L across the portfolio, abs + %, with a
per-position breakdown.

### `get_orders`

> ✅ Paginated (`orders`)

Chronological order history.

---

## Strategy tools

### `list_strategies`

> ✅ Paginated (`strategies`)

Installed strategies: version, description, author, symbols, defaults.

### `get_strategy_details`

Full metadata for one strategy.

| Property | Type | Required | Notes |
|---|---|---|---|
| `strategy_name` | string | ✅ | As returned by `list_strategies`. |

---

## Market-data & analytics tools

### `get_market_data`

> ✅ Paginated (`bars`) · Required: `symbol`

OHLCV bars for a symbol + period + interval.

| Property | Type | Required | Notes |
|---|---|---|---|
| `symbol` | string | ✅ | 1–16 chars. |
| `interval` | enum | — | `1m\|5m\|15m\|1h\|1d\|1wk\|1mo`, default `1d`. |
| `period` | string | — | Default `1y` (e.g. `6mo`, `3mo`, `1mo`). |
| `limit` / `cursor` | — | — | Pagination. |

### `get_cost_model`

> Required: `symbol`, `quantity`, `price`

Transaction-cost breakdown estimate (commission, spread, slippage, fee,
tax).

| Property | Type | Required | Notes |
|---|---|---|---|
| `symbol` | string | ✅ | 1–16 chars. |
| `quantity` | integer | ✅ | `≥ 1`. |
| `price` | number | ✅ | `> 0`. |
| `side` | enum | — | `buy\|sell`, default `buy`. |
| `avg_volume` | integer | — | Default `0`; for the slippage estimate. |

### `get_performance_metrics`

> Required: `equity_curve`

Compute total return, annualized return, Sharpe, Sortino, max drawdown,
win rate, and profit factor from an equity curve.

| Property | Type | Required | Notes |
|---|---|---|---|
| `equity_curve` | array | ✅ | ≥2 `{timestamp, total_value}` points. |
| `initial_capital` | number | — | Default `100000`, `> 0`. |

---

## Summary table

| Tool | Role | Paginated | Required |
|---|---|---|---|
| `run_backtest` | `quant_dev` | — | `strategy_name`, `symbol`, `start_date`, `end_date` |
| `get_portfolio_status` | `viewer` | — | — |
| `get_positions` | `viewer` | — | — |
| `get_position` | `viewer` | — | `symbol` |
| `get_unrealized_pnl` | `viewer` | — | — |
| `get_orders` | `viewer` | ✅ `orders` | — |
| `list_strategies` | `viewer` | ✅ `strategies` | — |
| `get_strategy_details` | `viewer` | — | `strategy_name` |
| `get_market_data` | `viewer` | ✅ `bars` | `symbol` |
| `get_cost_model` | `viewer` | — | `symbol`, `quantity`, `price` |
| `get_performance_metrics` | `viewer` | — | `equity_curve` |

## See also

- [`api-surface-map.yaml`](api-surface-map.yaml) — machine-readable.
- [Capability audit](capability-audit.md) — whole-surface review.
- [MCP server](../mcp-server.md) — design narrative.
- [`engine/mcp/tool_definitions.py`](../../engine/mcp/tool_definitions.py)
  — the code this catalog describes.
