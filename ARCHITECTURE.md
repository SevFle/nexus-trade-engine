# Nexus Trade Engine — Architecture

**Authoritative.** The engine treats this document as the reference for component boundaries, data flow, and system design. It will not redefine the architecture.

---

## System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         PRESENTATION LAYER                          │
│  React 18 + Vite 5 + Tailwind CSS 3.4 + Recharts + TanStack Query │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ REST + WebSocket
┌──────────────────────────────┴──────────────────────────────────────┐
│                          API GATEWAY LAYER                          │
│  FastAPI + JWT/RBAC + Rate Limiting + CORS + Security Headers      │
│  21 route groups │ 18 event types │ 7 auth providers               │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
┌──────────────────────────────┴──────────────────────────────────────┐
│                          CORE ENGINE LAYER                          │
│  OrderManager ─→ CostModel ─→ RiskEngine ─→ ExecutionBackend       │
│  Portfolio (tax lots) │ BacktestRunner │ EventBus (Redis pub/sub)   │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
┌──────────────────────────────┴──────────────────────────────────────┐
│                         PLUGIN SYSTEM LAYER                         │
│  PluginRegistry │ StrategySandbox (5-layer security) │ Marketplace  │
│  BaseStrategy (engine) │ IStrategy (SDK) │ ScoringStrategies        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
┌──────────────────────────────┴──────────────────────────────────────┐
│                           DATA LAYER                                │
│  TimescaleDB + PostgreSQL 16 │ Valkey 8 │ 8 Data Providers         │
│  17 ORM Models │ Alembic Migrations │ Event Bus (Redis)             │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Component Reference

### OrderManager (`engine/core/order_manager.py`)

Central signal-to-fill pipeline. All trades flow through this.

```
Signal → Create Order → Validate → Cost Estimate → Cost Tolerance Check
       → Risk Check → Submit → Execute → Reconcile → Update Portfolio
```

- `OrderStatus` enum: PENDING → VALIDATED → COSTED → RISK_APPROVED → SUBMITTED → FILLED (or REJECTED/RISK_REJECTED/FAILED/CANCELLED)
- `OrderType` enum: MARKET, LIMIT, STOP, STOP_LIMIT
- After fill: BUY calls `portfolio.open_position()`, SELL calls `portfolio.close_position()`
- Swappable execution backend via `set_execution_backend()`

### CostModel (`engine/core/cost_model.py`)

`ICostModel` (ABC) — passed into every strategy's `evaluate()` call.

| Method | Returns |
|--------|---------|
| `estimate_commission(symbol, qty, price)` | `Money` |
| `estimate_spread(symbol, price, side)` | `Money` |
| `estimate_slippage(symbol, qty, price, avg_volume)` | `Money` |
| `estimate_total(...)` | `CostBreakdown` |
| `estimate_pct(symbol, price, side)` | float |
| `estimate_tax(symbol, sell_price, qty, lots, method)` | `Money` |
| `check_wash_sale(symbol, sell_date, buy_history)` | bool |
| `calculate_wash_sale_adjustment(...)` | `WashSaleResult` |

`CostBreakdown`: commission + spread + slippage + exchange_fee + tax_estimate + currency_conversion → `total`

`DefaultCostModel`: configurable rates (spread_bps=5, slippage_bps=10, ST tax=37%, LT tax=20%, wash sale 30-day window).

### Portfolio (`engine/core/portfolio.py`)

Full tax-lot tracking with FIFO/LIFO/SPECIFIC_LOT methods.

- `open_position()` — deducts cash, creates tax lot, applies wash sale cost basis adjustment
- `close_position()` — consumes lots (FIFO/LIFO), calculates realized P&L, records sell for wash sale detection
- `snapshot()` — returns immutable `PortfolioSnapshot` for strategy evaluation
- `TaxMethod` enum: FIFO, LIFO, SPECIFIC_LOT
- `TaxLot`: symbol, quantity, purchase_price, purchase_date, `is_long_term()` (>= 365 days)

### RiskEngine (`engine/core/risk_engine.py`)

Pre-trade risk validation with final veto authority.

| Rule | Default |
|------|---------|
| Circuit breaker | 10% drawdown triggers halt |
| Max open positions | 50 |
| Position concentration | 20% |
| Single order value cap | $50,000 |
| Daily trade limit | 100 |

`check_order()` returns `RiskCheckResult(approved, reason, warnings)`.

### BacktestRunner (`engine/core/backtest_runner.py`)

Orchestrates the full backtest loop:

1. Fetch OHLCV data via provider
2. Create Portfolio + CostModel + RiskEngine + BacktestBackend + OrderManager + StrategySandbox
3. For each timestamp: build MarketState → update prices → sandbox.safe_evaluate() → process signals
4. Compute `PerformanceMetrics` (Sharpe, Sortino, max drawdown, win rate, profit factor, cost drag)
5. Run `StrategyEvaluator` scoring
6. Return `BacktestResult` with equity curve, trades, metrics

`BacktestSummary`: 25+ computed fields (annualized return, Sharpe, Sortino, Calmar, max drawdown duration/recovery, turnover, exposure).

### StrategySandbox (`engine/plugins/sandbox.py`)

Five-layer security model for untrusted plugin execution:

| Layer | Mechanism |
|-------|-----------|
| Import restrictions | `RestrictedImporter` blocks dangerous modules |
| Network whitelist | `SandboxedHttpClient` only declared endpoints |
| Resource limits | `resource.setrlimit` for memory + FDs |
| Filesystem isolation | Temp working dir, read-only, `builtins.open` replaced |
| Introspection blocking | `builtins.getattr` patched, `builtins.object` replaced |

`safe_evaluate()` never crashes the engine — returns `[]` on any error.

### PluginRegistry (`engine/plugins/registry.py`)

Discovers strategies from `strategies/*/manifest.yaml`, loads via `importlib`.

- `discover_strategies(base_dir)` → scans manifests
- `load_strategy_class(module_path)` → dynamic import
- `PluginRegistry.load_strategy(name)` → instantiate + return

### EventBus (`engine/events/bus.py`)

Async pub/sub with Redis backend + in-process fallback.

18 event types: MARKET_DATA_UPDATE, SIGNAL_EMITTED, ORDER_CREATED/FILLED/REJECTED, PORTFOLIO_UPDATED, POSITION_OPENED/CLOSED, CIRCUIT_BREAKER, BACKTEST_COMPLETED, etc.

### DataProviderRegistry (`engine/data/providers/registry.py`)

Priority-based provider routing with failover:

1. Filter enabled providers matching asset class + capability
2. Try candidates in priority order
3. `TransientProviderError` → fail-over to next provider
4. `FatalProviderError` → skip
5. Empty result → "soft miss", try next
6. All failed → raise `NoProviderAvailableError`

8 provider adapters: Yahoo (default), Polygon, Alpaca, Binance, CoinGecko, OANDA, + 2 more.

---

## Database Models (17 SQLAlchemy models)

All PKs are `uuid.UUID`. All financial values use `Numeric(18,4)`.

| Model | Table | Purpose |
|-------|-------|---------|
| User | users | Auth + profile, MFA, role enum |
| Portfolio | portfolios | User portfolios, initial_capital |
| Position | positions | Open positions, avg cost, current price |
| Order | orders | Order lifecycle, status history |
| InstalledStrategy | installed_strategies | Strategy-plugin mapping, JSONB config |
| WebhookConfig | webhook_configs | Webhook subscriptions, signing secrets |
| WebhookDelivery | webhook_deliveries | Delivery tracking + retry |
| BacktestResult | backtest_results | Metrics (JSONB), composite score |
| TaxLotRecord | tax_lot_records | Tax lot tracking, cost basis adjustment |
| OHLCVBar | ohlcv_bars | Historical price data |
| LegalDocument | legal_documents | Versioned legal docs |
| LegalAcceptance | legal_acceptances | User acceptance tracking |
| DataProviderAttribution | data_provider_attributions | Provider attribution |
| RefreshToken | refresh_tokens | JWT refresh tokens |
| ScoringSnapshot | scoring_snapshots | Strategy scoring results |
| DSRequest | dsr_requests | GDPR/CCPA data subject requests |
| ApiKey | api_keys | Long-lived API keys with scopes |

---

## API Endpoints (21 route groups)

| Prefix | Auth | Purpose |
|--------|------|---------|
| `/` | Public | Health check |
| `/metrics` | Public | Prometheus metrics |
| `/legal` | Public | Legal documents |
| `/api/v1/auth` | Public | Registration, login, refresh |
| `/api/v1/auth/mfa` | User | TOTP enrollment, verify |
| `/api/v1` | User | API key CRUD |
| `/api/v1` | User | System info |
| `/api/v1` | User | DSR/GDPR requests |
| `/api/v1` | User | WebSocket connections |
| `/api/v1/backtest` | **Legal gate** | Run backtests |
| `/api/v1/client` | Public | Frontend error reporting |
| `/api/v1/portfolio` | User | Portfolio management |
| `/api/v1/strategies` | User | Strategy CRUD |
| `/api/v1/webhooks` | User | Webhook management |
| `/api/v1/marketplace` | User | Strategy marketplace |
| `/api/v1/reference` | User | Instrument search |
| `/api/v1/tax` | User | Tax lot reporting |
| `/api/v1/scoring` | **Legal gate** | Strategy scoring |
| `/api/v1/market-data` | **Legal gate** | Market data |

---

## Plugin Interface

### SDK (`nexus_sdk`) — for third-party developers

```python
from nexus_sdk import IStrategy, Signal, MarketState, PortfolioSnapshot, ICostModel

class MyStrategy(IStrategy):
    @property
    def id(self) -> str: return "my-strategy"

    async def initialize(self, config: StrategyConfig) -> None: ...

    async def evaluate(self, portfolio: PortfolioSnapshot,
                       market: MarketState, costs: ICostModel) -> list[Signal]:
        if costs.estimate_pct("AAPL", 150.0, "buy") > 0.005:
            return [Signal.hold("AAPL")]
        return [Signal.buy("AAPL", weight=0.7)]

    async def dispose(self) -> None: ...
```

### Engine-side (`BaseStrategy`) — for internal plugins

```python
from engine.plugins.sdk import BaseStrategy

class Strategy(BaseStrategy):
    name = "my-strategy"
    version = "1.0.0"

    def on_bar(self, state, portfolio) -> list[dict]:
        return [{"symbol": "AAPL", "side": "buy", "weight": 0.7}]
```

---

## Middleware Stack

```
Request → SecurityHeaders → CORS → RateLimit → BodySizeLimit (1MB)
        → CorrelationId → HttpMetrics → Route Handler → Response
```

## Auth Architecture

`AuthProviderRegistry` supports simultaneous multi-provider auth:

| Provider | Module | Use Case |
|----------|--------|----------|
| Local | `local.py` | Email/password + bcrypt |
| Google | `google.py` | Google OAuth2 |
| GitHub | `github_oauth.py` | GitHub OAuth2 |
| OIDC | `oidc.py` | Keycloak, Auth0, Okta, Azure AD |
| LDAP | `ldap.py` | Active Directory |
| API Keys | `api_keys.py` | Long-lived scoped keys |
| MFA | `mfa_service.py` | TOTP-based |

## Observability Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Logging | structlog | JSON structured logs with correlation IDs |
| Tracing | OpenTelemetry | Distributed tracing |
| Metrics | Prometheus | Custom metrics + HTTP metrics |
| Errors | Sentry | Error tracking + breadcrumbs |
| Dashboards | Grafana | Pre-built dashboards as code |

## Data Flow: Signal to Fill

```
Strategy.evaluate()
    ↓ list[Signal]
OrderManager.process_signal()
    ↓ validate → cost estimate → risk check
RiskEngine.check_order()
    ↓ approved
ExecutionBackend.execute()
    ↓ filled
OrderManager._reconcile()
    ↓ update portfolio
Portfolio.open_position() / close_position()
    ↓ tax lot tracking + wash sale detection
EventBus.emit(ORDER_FILLED)
    ↓
WebSocket → Frontend
WebhookDispatcher → External systems
```

## Design Constraints

- **Async-first** — all I/O via asyncpg, httpx, taskiq. No blocking in hot paths.
- **Cost-first** — `ICostModel` is always available to strategies. No blind trading.
- **Sandboxed plugins** — untrusted code cannot escape the 5-layer sandbox.
- **Legal gates** — backtest/scoring/market-data endpoints require legal acceptance.
- **70% coverage gate** — enforced in CI via pytest-cov.
- **Ruff + basedpyright** — linting and type checking in CI.
