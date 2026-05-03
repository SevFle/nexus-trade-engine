# Nexus Trade Engine — Product Definition

**Authoritative.** The engine treats this document as the source of truth for what Nexus is and what it does. It will never redefine the product.

---

## What Nexus Is

Nexus is an **AI-native plugin trading framework with full cost modeling**. A modular, plugin-driven algorithmic trading platform designed for steady portfolio growth where transaction costs, taxes, slippage, spread, and wash sale rules are first-class citizens — not afterthoughts.

Strategies that backtest well must also perform in production. Nexus achieves this by passing the cost model directly into every strategy's `evaluate()` call, so strategies factor in real-world friction before emitting signals.

## Target Users

| Persona | Primary Need |
|---------|-------------|
| **Quant Developer** | Build, backtest, and deploy algorithmic strategies with a real cost model. Full control via SDK and CLI. |
| **Retail Trader** | Run pre-built strategies from the marketplace, monitor portfolio performance. Zero-code entry. |
| **Portfolio Manager** | Monitor risk, allocations, and performance across multiple strategies and portfolios. |

## Core Differentiators

1. **Cost model as input, not afterthought** — `ICostModel` is passed into every `evaluate()` call. Strategies see commissions, spread, slippage, taxes, and wash sale impact before deciding to trade.
2. **Plugin-first architecture** — Strategies are self-contained plugins implementing `IStrategy`. Developers have complete freedom: fixed algorithms, neural networks, LLM calls, or any hybrid.
3. **Three execution modes, one interface** — Backtest, Paper Trade, and Live Trade all use the same `IStrategy.evaluate()` interface. No code changes between modes.
4. **Tax-lot accounting** — Full FIFO/LIFO/Specific Lot tracking with wash sale detection and cost basis adjustments. Tax is a first-class concern.
5. **Sandboxed plugins** — Five-layer security model: import restrictions, network whitelist, resource limits, filesystem isolation, introspection blocking.

## Five-Layer Architecture

```
Presentation  →  React dashboard, WebSocket real-time streams
API Gateway   →  FastAPI, JWT/RBAC, rate limiting, correlation IDs
Core Engine   →  Order manager, cost model, risk engine, portfolio, tax lots
Plugin System →  SDK, registry, sandboxed runtime, strategy marketplace
Data Layer    →  TimescaleDB, PostgreSQL, Valkey (Redis), pluggable data providers
```

## Three Execution Modes

| Mode | Data | Execution | Use Case |
|------|------|-----------|----------|
| **Backtest** | Historical OHLCV | Simulated fills (98% fill prob, configurable slippage) | Strategy development, parameter tuning |
| **Paper Trade** | Live market data | Simulated execution | Forward testing, validation |
| **Live Trade** | Live market data | Real broker fills (Alpaca, IBKR, etc.) | Production trading |

All three modes use the same `ExecutionBackend` interface — swappable via `set_execution_backend()`.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Engine | Python 3.12, FastAPI, Pydantic v2 |
| Database | PostgreSQL 16 + TimescaleDB |
| Cache | Valkey 8 (Redis-compatible) |
| Task Queue | Taskiq + Redis broker |
| Frontend | React 18, Vite 5, Tailwind CSS 3.4, Recharts |
| Observability | Structlog, OpenTelemetry, Prometheus, Sentry |
| Testing | pytest, pytest-asyncio, Playwright, hypothesis |
| Containerization | Docker, Docker Compose |

## Key Invariants

- **Cost-first design** — No signal reaches the order manager without passing through the cost model.
- **Async-first** — All I/O is async (asyncpg, httpx, taskiq). No blocking calls in the hot path.
- **Plugin isolation** — Untrusted strategy code runs inside the `StrategySandbox`. Engine never trusts plugin output.
- **Multi-provider auth** — Local, Google OAuth, GitHub OAuth, OIDC (Keycloak/Auth0/Okta/Azure AD), LDAP. All simultaneously active.
- **Legal gate** — Backtest, scoring, and market data endpoints require legal document acceptance.
- **Quality gate** — 70% test coverage enforced in CI. Ruff linting. basedpyright type checking.

## What Nexus Is NOT

- Not a high-frequency trading platform (yet — Phase 7 research)
- Not a broker — it connects to brokers via adapters
- Not a financial advisor — strategies are user-authored plugins
- Not custodial — user API keys stay on their infrastructure

## Success Metrics

- Strategies that backtest profitably remain profitable in paper trading (cost model fidelity)
- Plugin developers can publish a strategy in under 30 minutes
- Zero unauthorized data access (sandbox guarantees)
- Sub-second WebSocket latency for real-time updates
