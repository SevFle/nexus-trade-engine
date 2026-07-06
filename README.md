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
- Python 3.11+ and [uv](https://github.com/astral-sh/uv)
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

## Documentation

Engineering documentation lives in [`docs/`](docs/README.md) and is
written for engineers who will read the source alongside the prose.
It is built with MkDocs Material (`uv sync --extra docs && uv run mkdocs
serve` for a live preview). Start with the [docs index](docs/README.md)
for a reading-order map; key entry points:

- [Architecture overview](docs/architecture/overview.md) — system
  components, request lifecycle, configuration.
- [API reference](docs/api-reference.md) — every HTTP and WebSocket
  route, auth model, error semantics.
- [MCP server](docs/mcp-server.md) — the Model Context Protocol surface
  for LLM agents (tools, resources, auth, config).
- [Data model](docs/data-model.md) — entities, relationships,
  invariants.
- [Development setup](docs/development.md) — local stack, tests,
  lint loop.
- [Deployment](docs/deployment.md) — infra requirements, env vars,
  rollout & rollback.
- [Known limitations & tech debt](docs/known-limitations.md) —
  honest, ranked list of what's half-built.
- [Runbooks](docs/operations/runbooks/README.md) — on-call debug
  guides, per-SLO and common-issue.
- [ADRs](docs/adr/README.md) — architecture decision records.

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Engine | Python 3.11, FastAPI, TaskIQ |
| Database | PostgreSQL 16, TimescaleDB |
| Cache / Broker | Valkey 8 (Redis-compatible) |
| Frontend | React 18, Vite, Tailwind CSS |
| Task Queue | TaskIQ + Valkey broker |
| Containerization | Docker (distroless runtime), Docker Compose |
| Testing | pytest, pytest-asyncio, Hypothesis |
| Lint / Types | Ruff, basedpyright |

## Roadmap

The honest, source-of-truth status lives in
[`docs/known-limitations.md`](docs/known-limitations.md); this list is
the headline view. Items marked *partial* have code landed but are not
on the public API surface or not production-validated.

- [x] Core architecture scaffold
- [x] Backtest engine with full cost model (commissions, spread,
      slippage, taxes, wash-sale, holding costs)
- [ ] Plugin SDK v1.0 *(in progress — `sdk/nexus_sdk/` ships
      `IStrategy`/`Signal`/types/testing, marketplace install is a stub)*
- [~] Paper trading execution *(partial — `engine/core/execution/paper.py`
      backend landed; not yet wired to a public run route)*
- [~] Live broker integration *(partial — concrete `LiveExecutionBackend`
      (`engine/execution/`, Alpaca-compatible REST, SEV-223) +
      `AlpacaTradingClient` (`engine/core/brokers/alpaca/`) landed and
      unit-tested; neither is registered in the execution factory nor wired
      to a run route — read-only `AlpacaDataProvider` is the only reachable
      Alpaca surface today; see
      [known-limitations](docs/known-limitations.md))*
- [ ] Strategy marketplace *(stub routes only — returns `not_implemented`)*
- [~] Multi-asset support *(partial — typed `Instrument` value object +
      `InstrumentAssetClass` enum landed in `engine/core/instruments.py` (#1213);
      the engine's internal `Signal` auto-derives it from `symbol`. Engine-
      internal only today: not surfaced in the public SDK, not persisted —
      positions/orders/tax lots still key on string `symbol`. See
      [ADR-0010](docs/adr/0010-instrument-asset-class-taxonomy.md) and the
      [instrument-modeling section](docs/architecture/core-domains.md))*
- [~] WebSocket real-time streams *(partial — see
      [known-limitations](docs/known-limitations.md): connection registry
      is process-local)*
- [~] MCP server *(partial — tools, resources, and auth are implemented
      in [`engine/mcp/`](docs/mcp-server.md); the runnable transport entry
      point (`server.py`) is not yet on disk, so the surface cannot be
      started today)*
- [ ] React dashboard MVP *(missing)*
- [ ] Observability export *(OpenTelemetry/Prometheus/Sentry wired but
      SLI metric coverage incomplete — see [SLO doc](docs/operations/slos.md))*

## License

MIT License — see [LICENSE](LICENSE) for details.
