# ADR-0005: Plugin sandbox via restricted-importer policy

**Status:** Accepted
**Date:** 2026-05-25
**Tracks:** SEV-266 (plugin runtime hardening), gh#33 (sandbox design)

## Context

Strategies are third-party Python code. Two failure modes are
unacceptable:

1. A buggy or malicious strategy crashes the engine process.
2. A strategy reaches out of its declared scope — reading the
   user table, hitting arbitrary network endpoints, or importing
   `subprocess`.

The naive fix (run every strategy in a subprocess) is too slow
for the backtest hot loop. The pythonic fix (an exec sandbox like
RestrictedPython) is well-understood but produces cryptic
tracebacks when the strategy imports anything non-trivial.

We needed something in between: fast enough to call inside the
backtest inner loop, restrictive enough to make exfiltration
provable-impossible at the policy level.

## Decision

Adopt a **two-layer sandbox**:

1. **Static policy** — every strategy ships a `strategy.manifest.yaml`
   that declares:
   - `runtime: python:3.11`
   - `network.allowed_endpoints: [...]`
   - `resources.max_memory`, `gpu`
   - `dependencies: [...]`

2. **Runtime enforcement** —
   [`engine/plugins/restricted_importer.py`](../../engine/plugins/restricted_importer.py)
   installs a custom import hook before the strategy's
   `evaluate()` runs. The hook:

   - Allows a fixed allowlist (`numpy`, `polars`, `pandas`,
     `httpx`, `pydantic`, the SDK itself, and any package the
     manifest declared as a dependency).
   - Blocks `subprocess`, `socket`, `ctypes`, `multiprocessing`,
     `os.system`, `shutil`, and the engine's own modules (so the
     strategy can't `import engine.db.models` to read the user
     table).
   - For `httpx`/`urllib`, replaces the transport with
     [`SandboxedHttp`](../../engine/plugins/sandboxed_http.py)
     which only permits calls to `network.allowed_endpoints`.

3. **Timeout + memory cap** — `evaluate()` runs in a
   `signal.alarm()` / `resource.setrlimit()` envelope. On timeout
   the strategy's `dispose()` is called and the engine returns
   the last good signal set (or an empty list).

The sandbox **does not** use full process isolation. The strategy
shares the engine's event loop, file descriptors (read-only on
its own bundle), and Postgres connection pool — but only through
the API the engine deliberately exposes.

## Consequences

**Positive**
- No subprocess fork per evaluation. Backtests stay fast.
- Policy is declarative: the manifest is auditable before the
  strategy is ever run. Marketplace install can reject manifests
  that ask for `subprocess` before the user sees them.
- Strategies can still use modern Python data tooling (numpy,
  polars, torch) without falling back to a sandboxed subset.

**Negative**
- A strategy that crashes the event loop (e.g. `os._exit`) can
  take the worker down. Mitigation: run the worker under
  systemd / k8s with `Restart=on-failure`. We do not promise
  survivability against malicious native code (ctypes would do
  it; that's why ctypes is blocked).
- The import hook is CPython-specific. Other Python
  implementations are not supported.
- Resource limits are best-effort on Linux (`RLIMIT_AS`); they
  do not perfectly cap RSS, especially with mmap'd numpy arrays.

## Alternatives considered

- **Subprocess per strategy** — too slow for backtest inner loop.
  Considered running the *whole backtest* in a subprocess and
  streaming signals back; rejected because it doubles IPC cost.
- **RestrictedPython** — too restrictive for the strategies we
  want to support (ML inference with torch needs full Python).
- **WASM sandbox (pyodide / wasmtime)** — interesting but adds
  ~50 ms cold start per evaluation. Revisit if/when WASM
  runtimes get a < 1 ms warm path.
- **Firecracker / gVisor micro-VM** — operationally too heavy
  for self-hosted operators.

## Open questions

- GPU resource isolation. Today's policy is `gpu: none | any`;
  multi-tenant GPU sharing is out of scope.
- A strategy that legitimately needs `multiprocessing` (e.g. for
  parallel parameter sweeps) cannot run in the sandbox. The
  planned escape hatch is a "trusted" trust-level flag set by an
  admin after audit; see `engine/plugins/trust_levels.py`.
