# Plugins

The plugin system lets operators extend Nexus Trade Engine with new
strategies, data providers, and execution backends without forking
the core. Plugins live under [`engine/plugins/`](../../engine/plugins/)
and are loaded via a runtime registry.

The interactive diagram in
[`plugin-sdk-architecture.jsx`](plugin-sdk-architecture.jsx) shows
the lifecycle visually; this doc is the flat-markdown source of truth.

## What can be a plugin

| Kind            | Loaded into                                | Example use case                          |
|-----------------|--------------------------------------------|-------------------------------------------|
| **Strategy**    | The backtest runner / live trading loop    | A custom signal generator, e.g. mean reversion. |
| **Data provider** | The data registry                        | A new market data source (broker REST, CSV, vendor SDK). |
| **Executor**    | The order pipeline                         | An adapter to a specific broker's REST/FIX/WS API. |
| **Webhook template** | The webhook dispatcher                | A new outbound payload shape (Slack/Discord-class). |

The plugin SDK's contract is intentionally small:

1. A class implementing the strategy interface — either the public
   [`IStrategy`](../../sdk/nexus_sdk/strategy.py) ABC (`async
   evaluate(portfolio, market, costs) -> list[Signal]`, what the README
   and [`PLUGIN_DEV_GUIDE.md`](../PLUGIN_DEV_GUIDE.md) teach) or the
   legacy in-engine [`BaseStrategy`](../../engine/plugins/sdk.py)
   (`on_bar(state, portfolio) -> list[dict]`).
2. A **YAML** manifest (`strategy.manifest.yaml` or `manifest.yaml`)
   parsed by the [`StrategyManifest`](../../engine/plugins/manifest.py)
   Pydantic model.
3. Optional `tests/` exercising the plugin against a fixture.

> The unified "every kind is a plugin" model below is the *target
> architecture*. Today only **strategies** are discoverable via the
> filesystem registry; data providers are registered in code from a YAML
> config (see [database/overview](overview.md)); executors are not yet
> pluggable.

## Discovery & registration

[`PluginRegistry`](../../engine/plugins/registry.py) walks a root
directory for `*/manifest.yaml` entries, loads each sibling
`strategy.py`, and instantiates its top-level `Strategy` class. The
canonical root is the repo-level [`strategies/`](../../strategies/)
directory. The [`StrategyManifest`](../../engine/plugins/manifest.py)
schema is what `strategy.manifest.yaml` (the filename the bundled
examples and the SDK use) is parsed against.

The HTTP surface in
[`routes/strategies.py`](../../engine/api/routes/strategies.py) talks
to `app.state.plugin_registry` (a richer registry exposing
`list_all()`, `get()`, `activate`/`reload`/`unload`). Note that today
this is only attached by the legacy
[`engine/main.py`](../../engine/main.py) entrypoint, not the canonical
`create_app()` in [`engine/app.py`](../../engine/app.py) — so live
strategy activation from the public API is part of the still-partial
strategy story (see [known-limitations.md](../known-limitations.md)).
This split is intentional: it lets the operator swap strategies in
config without re-deploying once the wiring is complete.

## Lifecycle

```
discover  ──▶  validate manifest  ──▶  import entry point  ──▶  register
                                                                    │
                                                                    ▼
                            domain code ◀───  registry.get(name) ───┤
                                                                    │
                                              shutdown ─────────────┤
                                                                    ▼
                                                     teardown / unregister
```

- **Discovery** is filesystem-walk based. No network calls; no
  arbitrary imports outside the plugin tree.
- **Validation** fails fast on missing fields, version mismatches, or
  declared dependencies that aren't installed.
- **Import** happens once at startup. If a plugin's import raises,
  the engine logs the error and continues without registering it —
  one bad plugin should not crash the service.
- **Teardown** runs at shutdown so plugins with sockets / threads
  close cleanly.

## Writing a plugin (strategy example)

Concrete tutorials live in
[`docs/PLUGIN_DEV_GUIDE.md`](../PLUGIN_DEV_GUIDE.md). The minimum
shape (matching the bundled [`strategies/`](../../strategies/)
examples):

1. Subclass the strategy interface — `nexus_sdk.IStrategy` for the
   public SDK contract (`async evaluate(...) -> list[Signal]`), or
   `engine.plugins.sdk.BaseStrategy` for the legacy `on_bar` loop.
2. Implement the required methods (`evaluate` for `IStrategy`,
   `on_bar` for `BaseStrategy`; `initialize`/`dispose`/`get_config_schema`
   for the SDK ABC).
3. Add a `strategy.manifest.yaml` (or `manifest.yaml`):
   ```yaml
   id: "mean-reversion-basic"
   name: "Mean Reversion Basic"
   version: "1.0.0"
   author: "Nexus Team"
   runtime: "python:3.11"
   dependencies: []
   resources:
     max_memory: "256MB"
     gpu: "none"
   network:
     allowed_endpoints: []
   config_schema:
     type: object
     properties:
       sma_period: { type: integer, default: 50 }
   data_feeds: ["ohlcv"]
   min_history_bars: 60
   watchlist: ["AAPL", "MSFT"]
   ```
4. Drop the directory at `strategies/<name>/` with a `strategy.py`
   that defines a `Strategy` class. See
   [`strategies/examples/`](../../strategies/examples/) for reference
   implementations (`mean_reversion`, `quality_momentum`,
   `llm_sentiment`).
5. Run the test suite — the plugin loader has its own integration
   tests that ensure your manifest validates and your entry point
   imports cleanly.

## Sandboxing

Strategies run inside a layered sandbox defined in
[`engine/plugins/sandbox.py`](../../engine/plugins/sandbox.py). Four of
five layers are implemented today; process isolation is the production
target and is tracked as a follow-up.

> **Important — threat model.** Layers 1–4 are **best-effort
> defense-in-depth, not a security boundary**. They narrow the
> accidental-bug surface and raise the bar for casual misuse, but they
> all run **in-process**, share the engine's memory and DB session,
> and can be defeated by a determined attacker with the full Python
> runtime available. **Only layer 5 (process / container isolation) is
> a real security boundary**, because it puts the strategy in a
> separate address space with the kernel enforcing the limits. Treat
> the four in-process layers accordingly — helpful guardrails, not
> something to stake a customer's data on.

| Layer | Kind | Status | Mechanism |
|---|---|---|---|
| 1. Import restrictions | best-effort in-process | **shipped** | `RestrictedImporter` blocks `subprocess`, `os.system`, `socket`, etc. unless the manifest declares them. |
| 2. Network whitelist | best-effort in-process | **shipped** | `SandboxedHttpClient` proxies every outbound call through an allowlist declared in the manifest (`requires_network: true` + URL prefixes). |
| 3. Resource limits | best-effort in-process | **shipped** | `resource.setrlimit` for memory / file descriptors on Linux. |
| 4. Filesystem isolation | best-effort in-process | **shipped** | Each evaluation runs in a fresh `tempfile.TemporaryDirectory`; the strategy only sees its own declared artifacts (read-only). |
| 5. Process isolation | **security boundary** | **planned** | Subprocess / container per strategy, communicated with via pipes (serialized `MarketState` in, `Signal[]` out). Killed on timeout / memory pressure. |

Because layers 1–4 are in-process, a malicious strategy that finds a
path past them can read environment variables, the database session,
and the filesystem. Operators must therefore treat plugins as part of
their trusted deployment surface today:

- Pin plugin versions in your operator config; do not auto-update.
- Plugins that come from third parties should be code-reviewed before
  install.
- For untrusted strategies, **do not rely on layers 1–4**. Run them
  in an external sandbox (container, VM) and call the engine through
  the SDK instead of loading them in-process.

Layer 5 is the production architecture — see the module docstring in
[`sandbox.py`](../../engine/plugins/sandbox.py). An ADR covering the
final isolation choice (likely WASI for strategies, separate process
for executors) is deferred until that work is sequenced.

## Where the code lives

| File                                        | Purpose                                |
|---------------------------------------------|----------------------------------------|
| [`strategies/`](../../strategies/)          | Strategy packages: each holds a `manifest.yaml` (+ optional artifacts) and a `strategy.py` exposing a `Strategy` class. `examples/` ships reference strategies. |
| [`engine/plugins/registry.py`](../../engine/plugins/registry.py) | Filesystem discovery + `PluginRegistry` (discover, load, instantiate). |
| [`engine/plugins/manifest.py`](../../engine/plugins/manifest.py) | `StrategyManifest` / `ResourceLimits` / `NetworkConfig` Pydantic schema + validation. |
| [`engine/plugins/sdk.py`](../../engine/plugins/sdk.py) | Legacy `BaseStrategy` ABC (`on_bar` loop). |
| [`sdk/nexus_sdk/strategy.py`](../../sdk/nexus_sdk/strategy.py) | Public `IStrategy` ABC (`evaluate` → `Signal[]`), `MarketState`, `StrategyConfig`. |
| [`engine/plugins/sandbox.py`](../../engine/plugins/sandbox.py) | Layered strategy sandbox (see below). |
| [`engine/plugins/restricted_importer.py`](../../engine/plugins/restricted_importer.py) | Allowlist import hook (layer 1). |
| [`engine/plugins/allowlist.py`](../../engine/plugins/allowlist.py) | The import allowlist itself. |
| [`engine/plugins/sandboxed_http.py`](../../engine/plugins/sandboxed_http.py) | Network-whitelist HTTP proxy (layer 2). |
| [`engine/plugins/scoring_executor.py`](../../engine/plugins/scoring_executor.py) | Runs `IScoringStrategy` plugins for the scoring routes. |
| [`tests/`](../../tests/)                    | Loader + manifest tests live alongside the rest of the suite (e.g. `test_plugin_registry.py`, `test_strategies_coverage.py`). |

Run `ls engine/plugins/` and `ls strategies/` to confirm the current
shape — these trees grow as new kinds land.
