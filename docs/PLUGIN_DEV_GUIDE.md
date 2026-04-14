# Plugin Developer Guide

Build and publish trading strategies for the Nexus Trade Engine.

## Quick Start

```bash
# Install the SDK
pip install nexus-trade-sdk

# Create your strategy directory
mkdir my-strategy && cd my-strategy
```

## The Golden Rule

**Input:** `PortfolioSnapshot` + `MarketState` + `ICostModel`
**Output:** `Signal[]`

Everything between input and output is yours. Use any algorithm, any model, any API. The engine only sees the signals.

## Strategy Structure

Every strategy needs two files:

```
my-strategy/
├── strategy.manifest.yaml   # Metadata, config, dependencies
└── strategy.py              # Your implementation
```

## Step 1: Create the Manifest

```yaml
id: "my-awesome-strategy"
name: "My Awesome Strategy"
version: "1.0.0"
author: "you@example.com"
description: "A brief description of what this strategy does."
license: "MIT"
min_engine_version: "0.1.0"

runtime: "python:3.11"
dependencies:
  - numpy>=1.26

resources:
  max_memory: "512MB"
  gpu: "none"

network:
  allowed_endpoints: []  # Add API URLs if calling external services

config_schema:
  type: object
  properties:
    lookback_period:
      type: integer
      default: 20
      min: 5
      max: 200
      description: "Number of bars to look back"

data_feeds:
  - "ohlcv"
min_history_bars: 30
watchlist: ["AAPL", "MSFT"]

marketplace:
  category: "algorithmic"
  tags: ["my-tag"]
  min_capital: 5000
```

## Step 2: Implement IStrategy

```python
from nexus_sdk import IStrategy, Signal, StrategyConfig, MarketState

class MyAwesomeStrategy(IStrategy):

    @property
    def id(self): return "my-awesome-strategy"

    @property
    def name(self): return "My Awesome Strategy"

    @property
    def version(self): return "1.0.0"

    async def initialize(self, config: StrategyConfig) -> None:
        self.lookback = config.params.get("lookback_period", 20)

    async def dispose(self) -> None:
        pass

    async def evaluate(self, portfolio, market, costs):
        signals = []
        for symbol in ["AAPL", "MSFT"]:
            price = market.latest(symbol)
            sma = market.sma(symbol, self.lookback)
            if price and sma and price < sma * 0.95:
                # Check if the trade is worth the cost
                cost_pct = costs.estimate_pct(symbol, price, "buy")
                expected_return = (sma - price) / price
                if expected_return > cost_pct * 2:
                    signals.append(Signal.buy(symbol, strategy_id=self.id))
        return signals

    def get_config_schema(self):
        return {"type": "object", "properties": {
            "lookback_period": {"type": "integer", "default": 20}
        }}
```

## Step 3: Test Locally

```python
from nexus_sdk.testing import StrategyTestHarness

async def test_my_strategy():
    harness = StrategyTestHarness(MyAwesomeStrategy())
    await harness.setup(params={"lookback_period": 20})

    # Simulate a tick
    signals = await harness.tick(prices={"AAPL": 140.0})

    # Assert behavior
    harness.assert_buy("AAPL", signals)
    await harness.teardown()
```

## Strategy Types

You can build ANY type of strategy:

### Fixed Algorithm
Deterministic rules. No external dependencies.
```python
# Moving average crossover, RSI, Bollinger Bands, etc.
```

### Machine Learning
Bundle trained models and run inference.
```python
async def initialize(self, config):
    import torch
    self.model = torch.load(config.params["model_path"])
```

### LLM-Powered
Call external AI APIs for reasoning.
```python
async def evaluate(self, portfolio, market, costs):
    import httpx
    response = await httpx.AsyncClient().post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": self._api_key},
        json={"model": "claude-sonnet-4-20250514", "messages": [...]}
    )
```

### Hybrid
Combine multiple approaches in one strategy.

## Cost-Aware Trading

The `costs` parameter in `evaluate()` lets you check if a trade is profitable after all fees:

```python
cost_pct = costs.estimate_pct(symbol, price, "buy")
if expected_return > cost_pct:
    signals.append(Signal.buy(symbol, strategy_id=self.id))
```

This prevents the classic trap of strategies that look great in backtests but lose money to transaction costs in production.

## Configuration Schema

Your `config_schema` in the manifest is a JSON Schema that the engine UI auto-renders as a settings form. Users can adjust parameters without touching code.

## Secrets

API keys and sensitive credentials go in `config.secrets`, which is encrypted at rest. Declare needed secrets in your documentation; users enter them in the UI.

## Publishing to Marketplace

```bash
# Package your strategy
nexus-sdk package ./my-strategy

# Publish (requires marketplace account)
nexus-sdk publish ./my-strategy-1.0.0.tar.gz
```

## Sandbox Restrictions

For security, strategies run in a sandboxed environment:

- **No filesystem access** (except bundled artifacts)
- **No raw network** (only endpoints declared in manifest)
- **Resource limits** (memory, CPU time as declared)
- **Timeout enforcement** (evaluate() is killed if it takes too long)
