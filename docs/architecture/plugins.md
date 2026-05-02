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

1. A class implementing the relevant Protocol from `engine/plugins/`.
2. A `manifest.toml` declaring name, version, kind, and entry point.
3. Optional `tests/` exercising the plugin against a fixture.

## Discovery & registration

At startup the engine scans `engine/plugins/<kind>/` for plugins that
declare a manifest. Discovered plugins are registered in the runtime
registry; the API and core code reference them by name (string id)
rather than by class import.

This is intentional: it lets the operator swap strategies / providers
in config without re-deploying.

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
shape:

1. Subclass the appropriate Protocol (e.g. `engine.plugins.strategy.Strategy`).
2. Implement the required methods (`on_bar`, `on_trade`, …).
3. Add a `manifest.toml`:
   ```toml
   [plugin]
   name = "mean-reversion"
   version = "0.1.0"
   kind  = "strategy"
   entry = "mean_reversion:Strategy"

   [plugin.dependencies]
   numpy = ">=2.0"
   ```
4. Drop the directory at `engine/plugins/strategy/mean_reversion/`.
5. Run the test suite — the plugin loader has its own integration
   tests that ensure your manifest validates and your entry point
   imports cleanly.

## Sandboxing

Plugins run in the same Python process as the engine; we do **not**
sandbox them with subprocess / WASM today. That means a malicious
plugin can read environment variables, the database, and the
filesystem.

Operators must therefore treat plugins as part of their trusted
deployment surface. Two practical implications:

- Pin plugin versions in your operator config; do not auto-update.
- Plugins that come from third parties should be code-reviewed before
  install.

A future ADR (deferred) will revisit this with a sandbox proposal —
likely WASI for strategies and a separate process for executors.

## Where the code lives

| File                                        | Purpose                                |
|---------------------------------------------|----------------------------------------|
| `engine/plugins/__init__.py`                | Public registry surface.               |
| `engine/plugins/manifest.py`                | Manifest schema + validation.          |
| `engine/plugins/loader.py`                  | Discovery + import logic.              |
| `engine/plugins/strategy/`                  | Strategy plugins live here.            |
| `engine/plugins/data/`                      | Data-provider plugins.                 |
| `engine/plugins/exec/`                      | Executor plugins.                      |
| `tests/plugins/`                            | Loader + manifest tests + fixtures.    |

(File names are illustrative — they may have evolved by the time you
read this. Run `tree engine/plugins/ -L 2` to see the current shape.)
