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
| `engine/plugins/__init__.py`                | Public registry surface.               |
| `engine/plugins/manifest.py`                | Manifest schema + validation.          |
| `engine/plugins/loader.py`                  | Discovery + import logic.              |
| `engine/plugins/strategy/`                  | Strategy plugins live here.            |
| `engine/plugins/data/`                      | Data-provider plugins.                 |
| `engine/plugins/exec/`                      | Executor plugins.                      |
| `tests/plugins/`                            | Loader + manifest tests + fixtures.    |

(File names are illustrative — they may have evolved by the time you
read this. Run `tree engine/plugins/ -L 2` to see the current shape.)
