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
[`engine/plugins/sandbox/__init__.py`](../../engine/plugins/sandbox/__init__.py).
Five of six layers (0–4) are implemented in-process today; layer 5
(process isolation) is the production security boundary and is tracked
as a follow-up. The numbering is historical: layers 1–4 were the
original in-process set; layer 0 (static AST validation) was added by
[ADR-0010](../adr/0010-static-ast-validation-toctou-loading.md), and
layer 5 remains the only true isolation boundary.

> **Important — threat model.** Layers 0–4 are **best-effort
> defense-in-depth, not a security boundary**. They narrow the
> accidental-bug surface and raise the bar for casual misuse, but they
> all run **in-process**, share the engine's memory and DB session,
> and can be defeated by a determined attacker with the full Python
> runtime available. **Only layer 5 (process / container isolation) is
> a real security boundary**, because it puts the strategy in a
> separate address space with the kernel enforcing the limits. Treat
> the in-process layers accordingly — helpful guardrails, not
> something to stake a customer's data on.

| Layer | Kind | Status | Mechanism |
|---|---|---|---|
| 0. Static source validation | best-effort in-process | **shipped** | [`ImportValidator`](../../engine/plugins/restricted_importer.py) walks the strategy's AST **before** compile/exec and rejects `import` of a blocked module, plus bare calls to `__import__`/`exec`/`eval`/`compile` and `importlib.import_module(…)`. An exact match in the blocked set can never be overridden by the allowlist. See [ADR-0010](../adr/0010-static-ast-validation-toctou-loading.md). |
| 0a. Runtime introspection blocking | best-effort in-process | **shipped** | `_BLOCKED_ATTRS` + `_restricted_getattr` (installed over `builtins.getattr` per evaluation) refuse the dynamic `getattr(obj, name)` form of every sandbox-escape dunder — type traversal (`__class__`/`__base__`/`__subclasses__`), function introspection (`__globals__`/`__code__`/`__closure__`), namespace access (`__builtins__`/`__dict__`), pickle gadget chains (`__reduce__`), module loading (`__loader__`/`__spec__`). `builtins.object` is swapped for `_RestrictedObject` so `object.__subclasses__()` raises. The 3-argument `getattr(obj, name, default)` contract is honoured (returns the caller's default, never the blocked value) so `inspect`/`dataclasses`/`pydantic` keep working. Direct dotted access (`obj.__class__`) is **not** intercepted — that is layer 5's job. See [ADR-0011](../adr/0011-runtime-introspection-blocking.md). |
| 1. Import restrictions | best-effort in-process | **shipped** | Default-deny **allowlist** (`FROZEN_ALLOWED_MODULES` in [`allowlist.py`](../../engine/plugins/allowlist.py)): a strategy may import a module only if its root name is in the frozen set. Enforced by [`RestrictedImporter`](../../engine/plugins/restricted_importer.py) at both `sys.meta_path` and `builtins.__import__`. The old denylist (`DENYLIST_MODULES`) is retained as defence-in-depth and as the test-suite's escape-vector oracle, but enforcement is purely allowlist-based. Adding a module requires a security review (see [ADR-0007](../adr/0007-strategy-sandbox-allowlist-imports.md)). |
| 2. Network whitelist | best-effort in-process | **shipped** | `SandboxedHttpClient` proxies every outbound call through an allowlist declared in the manifest (`requires_network: true` + URL prefixes). |
| 3. Resource limits | best-effort in-process | **shipped** | [`engine/plugins/sandbox/resource_limits.py`](../../engine/plugins/sandbox/resource_limits.py) wraps every `evaluate()` / `on_bar()` call in a `resource_limits(...)` context manager that enforces **two independent guards**: (a) **CPU time** via `signal.SIGALRM` / `setitimer` — kernel-delivered on the next bytecode boundary, so it preempts a tight loop that never `await`s (the asyncio `wait_for` can only fire on loop re-entry); (b) **memory cap** via `tracemalloc` peak/high-water snapshot plus a background daemon-thread poller that catches a sustained breach within `poll_interval`. A separate `RLIMIT_AS` kernel backstop (installed by the host sandbox, not this module) covers native/C-extension allocations `tracemalloc` cannot see (NumPy buffers, `mmap`, …) — the tracemalloc layer is a best-effort Python-visible trip-wire that fires a structured `SandboxResourceError(kind="memory")` *before* the harder `MemoryError`. Violations surface as `SandboxResourceError` — which inherits **directly from `BaseException`, not `Exception`** (gh#1545), so the canonical `except Exception: pass` defeat-attempt cannot swallow a SIGALRM-raised CPU violation — carrying structured `kind` / `limit` / `actual` metadata so dashboards can tell a CPU blow-up from a memory blow-up. A module-level non-reentrant `threading.Lock` serialises entry because `SIGALRM` handler slots and the tracemalloc peak counter are **process-global** — overlapping invocations would clobber each other's teardown; re-entrant or concurrent entry raises `SandboxResourceError(kind="single_flight")` instead of deadlocking or racing. See [ADR-0012](../adr/0012-sandbox-resource-limits-single-flight.md). |
| 4. Filesystem isolation | best-effort in-process | **shipped (active path) + reusable primitive landed, not yet wired** | The active sandbox (`_setup_filesystem_isolation` / `_restricted_open` in [`sandbox/__init__.py`](../../engine/plugins/sandbox/__init__.py)) runs each evaluation in a fresh `tempfile.TemporaryDirectory`, wraps `builtins.open`, and rejects any path whose `os.path.realpath` canonical form falls outside the work dir or a declared `manifest.artifacts` root. Writes, append, and `+` modes are rejected; raw file-descriptor `open`s are rejected. The check opens the **resolved** path, not the raw argument, so a symlink/relative path can't be reinterpreted by the kernel between check and use (TOCTOU). Separately, [`engine/plugins/sandbox/filesystem.py`](../../engine/plugins/sandbox/filesystem.py) ships a host-side, framework-agnostic [`PathValidator`](../../engine/plugins/sandbox/filesystem.py) (whitelist canonicalisation with exact / `<root>{sep}` prefix matching, write gating, `make_open_hook` / `make_path_hook` factories) that closes both symlink-traversal and `..` traversal in one `realpath` step. **`PathValidator` landed (#1436) but is not yet imported by the active sandbox** — the inline `_restricted_open` is what runs today. Unifying the two is a tracked follow-up. |
| 5. Process isolation | **security boundary** | **planned** | Subprocess / container per strategy, communicated with via pipes (serialized `MarketState` in, `Signal[]` out). Killed on timeout / memory pressure. |

Because layers 0–4 are in-process, a malicious strategy that finds a
path past them can read environment variables, the database session,
and the filesystem. Operators must therefore treat plugins as part of
their trusted deployment surface today:

- Pin plugin versions in your operator config; do not auto-update.
- Plugins that come from third parties should be code-reviewed before
  install.
- For untrusted strategies, **do not rely on layers 0–4**. Run them
  in an external sandbox (container, VM) and call the engine through
  the SDK instead of loading them in-process.

Layer 5 is the production architecture — see the module docstring in
[`sandbox/__init__.py`](../../engine/plugins/sandbox/__init__.py).
[ADR-0010](../adr/0010-static-ast-validation-toctou-loading.md) covers
the static-AST Layer 0 + the validate-then-exec loader; [ADR-0011](../adr/0011-runtime-introspection-blocking.md)
covers the runtime introspection guard (Layer 0a); [ADR-0012](../adr/0012-sandbox-resource-limits-single-flight.md)
covers the Layer-3 resource-limits design and the single-flight lock
that protects the process-global `SIGALRM` / `tracemalloc` state. An
ADR covering the final isolation choice (likely WASI for strategies,
separate process for executors) is deferred until that work is sequenced.

## Where the code lives

| File                                        | Purpose                                |
|---------------------------------------------|----------------------------------------|
| [`strategies/`](../../strategies/)          | Strategy packages: each holds a `manifest.yaml` (+ optional artifacts) and a `strategy.py` exposing a `Strategy` class. `examples/` ships reference strategies. |
| [`engine/plugins/registry.py`](../../engine/plugins/registry.py) | Filesystem discovery + `PluginRegistry` (discover, load, instantiate). |
| [`engine/plugins/manifest.py`](../../engine/plugins/manifest.py) | `StrategyManifest` / `ResourceLimits` / `NetworkConfig` Pydantic schema + validation. |
| [`engine/plugins/sdk.py`](../../engine/plugins/sdk.py) | Legacy `BaseStrategy` ABC (`on_bar` loop). |
| [`sdk/nexus_sdk/strategy.py`](../../sdk/nexus_sdk/strategy.py) | Public `IStrategy` ABC (`evaluate` → `Signal[]`), `MarketState`, `StrategyConfig`. |
| [`engine/plugins/sandbox/__init__.py`](../../engine/plugins/sandbox/__init__.py) | Layered strategy sandbox (see below). |
| [`engine/plugins/sandbox/resource_limits.py`](../../engine/plugins/sandbox/resource_limits.py) | Layer-3 `resource_limits(...)` context manager: SIGALRM CPU timer + `tracemalloc` memory cap + single-flight lock. Host-side module (imported before sandbox restrictions activate; `engine.*` is denied by the restricted importer so sandboxed code cannot reach it). |
| [`engine/plugins/sandbox/filesystem.py`](../../engine/plugins/sandbox/filesystem.py) | Reusable layer-4 `PathValidator` (whitelist + `realpath` canonicalisation, write gating, open/path hooks). Landed (#1436); **not yet wired** into the active sandbox, which uses the inline `_restricted_open` in `sandbox/__init__.py`. |
| [`engine/plugins/restricted_importer.py`](../../engine/plugins/restricted_importer.py) | Allowlist import hook (layer 1) + static `ImportValidator` AST checker (layer 0). |
| [`engine/plugins/allowlist.py`](../../engine/plugins/allowlist.py) | The import allowlist + denylist. |
| [`engine/plugins/sandboxed_http.py`](../../engine/plugins/sandboxed_http.py) | Network-whitelist HTTP proxy (layer 2). |
| [`engine/plugins/scoring_executor.py`](../../engine/plugins/scoring_executor.py) | Runs `IScoringStrategy` plugins for the scoring routes. |
| [`tests/`](../../tests/)                    | Loader + manifest tests live alongside the rest of the suite (e.g. `test_plugin_registry.py`, `test_strategies_coverage.py`). |

Run `ls engine/plugins/` and `ls strategies/` to confirm the current
shape — these trees grow as new kinds land.
