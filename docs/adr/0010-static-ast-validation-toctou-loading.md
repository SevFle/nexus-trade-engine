# ADR-0010: Static AST validation + TOCTOU-safe strategy loading

- **Status**: Accepted
- **Date**: 2026-07-07
- **Deciders**: Lead maintainer + security reviewer
- **Tags**: security, plugins, sandbox

## Context and Problem Statement

[ADR-0007](0007-strategy-sandbox-allowlist-imports.md) locked the import
policy down to a default-deny **allowlist** enforced at *runtime* via two
hooks (`sys.meta_path` + `builtins.__import__`). That closes the static
`import` statement, but it leaves two residual escape classes that the
runtime hooks alone cannot address:

1. **Code-execution / dynamic-import builtins.** A strategy can write
   `exec(payload)`, `eval(x)`, `compile(...)`, `__import__("os")`, or
   `importlib.import_module("os")`. None of these go through the static
   `import` statement the runtime hooks intercept — they construct and
   run code objects directly, so the allowlist never sees the module
   name being loaded. Catching them requires either intercepting the
   builtin at runtime (fragile, easy to miss new spellings) or rejecting
   the *call site* before the module body ever executes.

2. **Time-of-check / time-of-use (TOCTOU) in the loader.** The classic
   `importlib` path is *validate, then load* — but
   `spec.loader.exec_module(module)` **re-reads the file from disk**. An
   attacker who can swap `strategy.py` between the validation read and
   the exec read can get un-validated bytes executed: the validator saw
   a clean file, the loader ran a malicious one. For untrusted plugins
   dropped onto a shared filesystem this is a real window, however
   narrow.

Both gaps were surfaced and fixed together (gh#1239, gh#1245). This ADR
records *why* we chose static AST validation + validate-then-exec over
the alternatives, because the mechanism is non-obvious and the two
commits look like unrelated hardening when they are in fact one
defense-in-depth decision.

## Decision Drivers

- **Defence in depth, fail fast.** The runtime hooks are the last line;
  rejecting a bad strategy at *parse* time, before any of its body runs,
  is strictly safer and has no side effects to roll back.
- **No new runtime overhead on the hot path.** Validation runs once per
  load (startup), not per `evaluate()` call.
- **Don't reinvent execution.** We still want ordinary `importlib`
  semantics for the *namespace* (`__file__`, `__loader__`, tracebacks
  pointing at `strategy.py`); only the *byte-source* must be pinned.

## Considered Options

1. **Runtime-only: monkeypatch `exec`/`eval`/`compile`/`__import__`.**
2. **Static AST validation before compile, keep the standard loader.**
3. **Static AST validation *and* validate-then-exec the exact validated
   bytes** (compile the bytes ourselves, skip `exec_module`'s re-read).
4. **Switch to a separate-process loader now** (run each strategy in a
   subprocess that reads the file).

## Decision Outcome

Chosen option: **Option 3 — static AST validation + validate-then-exec
the exact bytes**, because it closes *both* residual classes without
introducing process-isolation complexity (still deferred to layer 5,
ADR-0007).

### Consequences

- **Positive** — a strategy smuggling `exec(payload)` or
  `importlib.import_module('os')` is rejected at load time with a
  precise `line N: call to forbidden builtin …` message, before the
  runtime hooks fire and before the module body executes.
- **Positive** — the TOCTOU window between validation and execution is
  eliminated: the loader reads the file **once** (as bytes), validates
  those bytes, then `compile()`s and `exec()`s the *same* bytes into the
  module namespace. There is no second disk read.
- **Positive** — the allowlist can never be defeated by a blocked module
  sneaking into it: an *exact* match in the blocked set short-circuits
  to "blocked" before the allowlist is consulted (the allowlist may only
  permit a *proper submodule* of a blocked root, never the root itself).
- **Negative** — a strategy that legitimately uses `eval` for, e.g., a
  small expression DSL is rejected. This is intentional (request a
  review / precompute), but it is real friction.
- **Neutral** — `SyntaxError` from `ast.parse` propagates to the caller
  rather than being swallowed; malformed source is itself a rejection,
  and reporting it is the loader's job, not the validator's.

## Details

### The validator — `ImportValidator(ast.NodeVisitor)`

Source: [`engine/plugins/restricted_importer.py`](../../engine/plugins/restricted_importer.py)
(`class ImportValidator`, `validate(source)`).

`ImportValidator` walks the parsed AST of a strategy's source **before**
it is compiled/executed and records violations for code that:

- imports a module not permitted by the policy (`visit_Import`,
  `visit_ImportFrom`); or
- invokes a code-execution / dynamic-import builtin (`visit_Call`).

Module policy (`_module_is_blocked`) precedence — a `True` result
short-circuits to "blocked":

| # | Case | Rule |
|---|------|------|
| 1 | Exact match in the blocked set | **Blocked.** The allowlist *cannot* override an exact match — this closes the "allowlist shadows a blocked module" bypass (an `os` entry sneaking into the allowlist is still rejected). |
| 2 | Proper submodule of a blocked root (`os.path` under a blocked `os`) | Blocked **unless** the full name is explicitly in the allowlist. The allowlist therefore applies only to proper submodules of a blocked root, never to the root itself. |
| 3 | Otherwise | Blocked iff the module's root is not in the allowlist (the ordinary allowlist gate from ADR-0007). |

Detected call sites (`visit_Call`): any bare-`Name` call to
`__import__`, `exec`, `eval`, or `compile`, and the qualified dynamic
import `importlib.import_module(...)`. Relative imports (`level > 0`
with no absolute module) are skipped — they resolve within the strategy
package and carry no cross-package escape vector at this layer.

The validator is **stateless across runs** — `violations` is reset on
every `validate()` call — so a single instance is safe to reuse.

### The loader — validate-then-exec the exact bytes

Source: [`engine/plugins/registry.py`](../../engine/plugins/registry.py)
(`load_strategy_class`).

```python
with open(module_path, "rb") as f:
    source_bytes = f.read()                      # single read
violations = ImportValidator(DENYLIST_MODULES).validate(source_bytes)
if violations:
    raise ImportError(f"... rejected by import validator: {joined}")

module = importlib.util.module_from_spec(spec)   # populates __file__/__loader__/__spec__
code = compile(source_bytes, module_path, "exec")
exec(code, module.__dict__)                      # the exact validated bytes
```

`spec.loader.exec_module(module)` is **deliberately avoided**: it
re-reads the file from disk, which would re-open the TOCTOU window.
Instead the validated bytes are compiled into a code object bound to the
module's file path (so tracebacks still point at `strategy.py`) and
`exec`'d straight into the module's namespace. The `module_from_spec`
call has already populated `__file__` / `__loader__` / `__spec__`, so
the module looks identical to one loaded the standard way — only the
byte-source is pinned.

This is a deliberate, audited `exec` call site (`# noqa: S102`); the
`S102` (flake8-bandit "use of exec") suppression is documented inline
precisely because removing the `exec` would require `exec_module`, which
re-opens the gap this code closes.

### Relationship to the runtime hooks (ADR-0007)

Layering, earliest to latest:

```
strategy.py on disk
   │
   ▼  read bytes ONCE
ImportValidator.validate(bytes)   ←── ADR-0010 (this): fail fast, side-effect free
   │  (rejected here ⇒ ImportError, module body never runs)
   ▼  compile + exec the SAME bytes into the module namespace
module body executes
   │
   ▼  every `import X` / `__import__(X)`
RestrictedImporter (meta_path + builtins.__import__)  ←── ADR-0007: runtime allowlist
   │  (rejected here ⇒ ImportError at the import site)
   ▼
strategy.evaluate(...) runs under layers 2–4 (network/fs/resource)
```

The static validator is the outer ring; the runtime hooks are the
inner ring. A strategy must pass **both** to run. Neither replaces
layer 5 (process/container isolation), which remains the only true
security boundary — see the threat-model note in
[`architecture/plugins.md`](../architecture/plugins.md#sandboxing).

## Pros and Cons of the Options

### Option 1 — Runtime-only monkeypatch of `exec`/`eval`/`compile`/`__import__`

- **Pros:** Catches dynamically-constructed code at the moment of
  execution.
- **Cons:** Fragile — every new spelling (`types.FunctionType(code, …)`,
  `pickle`, `marshal`) needs a fresh patch; the module body still runs
  up to the dangerous call, so side effects before the call are not
  prevented; high runtime overhead on the hot path.

### Option 2 — Static validation + standard loader

- **Pros:** Fails fast on the call-site class; no TOCTOU concern for
  the *validator*, which has already parsed its input.
- **Cons:** Leaves the loader TOCTOU gap — the validated file is not the
  file that gets executed, because `exec_module` re-reads.

### Option 3 — Static validation + validate-then-exec (chosen)

- **Pros:** Closes both the call-site class and the loader TOCTOU gap;
  zero hot-path overhead; module namespace looks standard.
- **Cons:** Requires the deliberate `exec` (suppressed with an inline
  justification); strategy authors lose `eval`/`exec`/`compile`/dynamic
  import as legitimate tools.

### Option 4 — Separate-process loader

- **Pros:** Strongest; the host never imports the strategy at all, so
  the gap is structurally absent.
- **Cons:** Premature — layer 5 (ADR-0007) is the production target and
  is not yet built. Building a loader subprocess now would duplicate
  that boundary and be thrown away.

## Links

- Block unsafe AST nodes + fix import bypass: gh#1239
- Close TOCTOU race in plugin loading: gh#1245
- Source:
  [`engine/plugins/restricted_importer.py`](../../engine/plugins/restricted_importer.py)
  (`ImportValidator`),
  [`engine/plugins/registry.py`](../../engine/plugins/registry.py)
  (`load_strategy_class`)
- Supersedes/extends: [ADR-0007](0007-strategy-sandbox-allowlist-imports.md)
  (runtime allowlist) — ADR-0007 remains Accepted; this ADR is the
  complementary outer ring.
- Related: [`docs/architecture/plugins.md`](../architecture/plugins.md)
