# ⚡ Nexus Trade Engine

**AI-native plugin trading framework with full cost modeling.**

Nexus is a modular, plugin-driven algorithmic trading platform designed for steady portfolio growth. It treats transaction costs, taxes, slippage, and spread as first-class citizens — not afterthoughts — ensuring strategies that backtest well also perform in production.

---

## Architecture

Nexus is built on a five-layer architecture where every component is independently scalable and replaceable:

| Layer | Purpose | Key Components |
|-------|---------|----------------|
| **Presentation** | Web dashboard & API consumers | React UI, WebSocket streams |
| **API Gateway** | Orchestration & auth | FastAPI, JWT/RBAC, rate limiting |
| **Core Engine** | Trade execution & risk | Order manager, cost model, risk engine |
| **Plugin System** | Strategy marketplace | SDK, registry, sandboxed runtime |
| **Data Layer** | Storage & market feeds | TimescaleDB, PostgreSQL, Redis |

Interactive architecture diagrams are available in [`docs/architecture/`](docs/architecture/).

## Core Concepts

### Plugin-First Strategy System

Strategies are self-contained plugins that implement the `IStrategy` interface. Developers have **complete freedom** in their implementation — fixed algorithms, neural networks, LLM calls, or any hybrid combination. The engine only cares about the signals that come out.

```python
from nexus_sdk import IStrategy, Signal

class MyStrategy(IStrategy):
    def evaluate(self, portfolio, market, costs):
        # Your logic here — anything goes
        return [Signal.buy("AAPL", weight=0.7)]
```

### Three Execution Modes, One Interface

Every strategy runs identically across all three modes:

- **Backtest** — Historical simulation with full cost model
- **Paper Trade** — Live market data, simulated execution
- **Live Trade** — Real money, real broker, same interface

### Cost Model as Input, Not Afterthought

The `ICostModel` is passed directly into every strategy's `evaluate()` call. Strategies can (and should) factor in commissions, spread, slippage, taxes (FIFO/LIFO), wash sale rules, and dividend withholding **before** emitting signals.

## Quick Start

### Prerequisites

- Docker & Docker Compose
- Python 3.12+ and [uv](https://github.com/astral-sh/uv)
- Node.js 20+
- Git

### Setup

```bash
# Clone the repository
git clone https://github.com/your-org/nexus-trade-engine.git
cd nexus-trade-engine

# Start infrastructure (Postgres/TimescaleDB, Valkey)
docker compose up -d db valkey

# Install engine dependencies (creates .venv from pyproject.toml + uv.lock)
uv sync --all-extras

# Run database migrations
.venv/bin/alembic upgrade head

# Seed sample market data
.venv/bin/python scripts/seed_data.py

# Start the engine
.venv/bin/uvicorn engine.app:create_app --factory --host 0.0.0.0 --port 8000
```

```bash
# In a separate terminal — start the frontend
cd frontend
npm install
npm run dev
```

### Install the SDK (for strategy developers)

```bash
pip install nexus-trade-sdk
# or from source:
cd sdk && pip install -e .
```

## Project Structure

```
nexus-trade-engine/
├── engine/                 # Core trading engine (FastAPI)
│   ├── core/               # Order management, portfolio, risk
│   │   └── execution/      # Backtest / Paper / Live backends
│   ├── plugins/            # Plugin SDK, registry, sandbox
│   ├── data/               # Market data feeds & providers
│   ├── events/             # Pub/sub event bus
│   ├── api/                # REST & WebSocket routes
│   └── db/                 # Models, migrations, session
├── sdk/                    # Installable SDK for strategy devs
│   └── nexus_sdk/          # IStrategy, Signal, types, testing
├── strategies/             # Example strategy plugins
│   └── examples/           # Reference implementations
├── frontend/               # React dashboard
├── tests/                  # Test suite
├── scripts/                # DB init, data seeding, utilities
├── docs/                   # Architecture docs & diagrams
└── docker-compose.yml      # Infrastructure stack
```

## Developing a Strategy Plugin

1. Install the SDK: `pip install nexus-trade-sdk`
2. Create a manifest file (`strategy.manifest.yaml`)
3. Implement the `IStrategy` interface
4. Test locally with the backtest runner
5. Publish to the marketplace

See the [Plugin Developer Guide](docs/PLUGIN_DEV_GUIDE.md) for full documentation.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Engine | Python 3.12, FastAPI, TaskIQ |
| Database | PostgreSQL 16, TimescaleDB |
| Cache / Broker | Valkey 8 (Redis-compatible) |
| Frontend | React 18, Vite, Tailwind CSS |
| Task Queue | TaskIQ + Valkey broker |
| Containerization | Docker (distroless runtime), Docker Compose |
| Testing | pytest, pytest-asyncio, ruff, basedpyright |

## Documentation

Full engineering documentation lives in [`docs/`](docs/README.md).
Start there for anything beyond the elevator pitch in this README.

| Audience | Start with |
|---|---|
| New engineers | [`docs/architecture/overview.md`](docs/architecture/overview.md) |
| API consumers | [`docs/api/README.md`](docs/api/README.md) |
| Operators | [`docs/deployment.md`](docs/deployment.md) → [`docs/operations/slos.md`](docs/operations/slos.md) |
| Strategy authors | [`docs/PLUGIN_DEV_GUIDE.md`](docs/PLUGIN_DEV_GUIDE.md) |
| Decision history | [`docs/adr/`](docs/adr/README.md) |
| Honest gaps | [`docs/known-limitations.md`](docs/known-limitations.md) |

## Roadmap

The live roadmap lives in [`STRATEGY.md`](STRATEGY.md). High-level
status as of the last audit:

- [x] Core architecture scaffold, plugin SDK, auth/RBAC, MFA, webhooks,
      backtest engine with full cost model, paper trading, legal
      acceptance, GDPR/CCPA DSR, multi-jurisdiction tax reports, k6
      load tests, structured logging + OpenTelemetry tracing, API key
      auth, WebSocket real-time push.
- [ ] Live broker integration (Alpaca / IBKR) — see
      [`docs/known-limitations.md`](docs/known-limitations.md).
- [ ] Multi-strategy orchestration.
- [ ] Strategy marketplace (registry + signing + install pipeline).
- [ ] Multi-asset support (crypto, forex, options).
- [ ] MCP server for LLM-driven strategy authoring.
- [ ] React dashboard parity with the API surface.

## License

MIT License — see [LICENSE](LICENSE) for details.
