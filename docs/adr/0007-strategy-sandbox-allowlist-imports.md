# ADR-0007: Strategy sandbox — allowlist import model

- **Status**: Accepted
- **Date**: 2026-06-16
- **Deciders**: Lead maintainer + security reviewer
- **Tags**: security, plugins, sandbox

## Context and Problem Statement

Nexus runs **third-party strategy plugins** inside the engine process.
Strategies are arbitrary Python that implements `IStrategy.evaluate()` —
they could be authored by marketplace contributors, not just the
operator. A malicious or buggy strategy must not be able to read the
filesystem, spawn processes, reach the network undeclared, or
exfiltrate secrets (`NEXUS_SECRET_KEY`, `NEXUS_MFA_ENCRYPTION_KEY`,
provider API keys).

The first-generation sandbox enforced imports via a **denylist** — a
`BLOCKED_MODULES` set of known-dangerous names (`os`, `subprocess`,
`socket`, …). Every import was allowed unless explicitly blocked. This
is an unwinnable arms-race: every new dangerous module shipped with
CPython (or a third-party package already on the import path) is a
fresh escape vector until someone remembers to add it to the denylist.
A dedicated security sweep (gh#908) patched critical escape vectors,
but the review itself proved the denylist approach was structurally
weak.

## Decision Drivers

- **Threat model.** Strategies are untrusted code running in-process.
  The import policy is the primary containment boundary before process
  isolation (layer 5) lands.
- **Maintenance cost.** The denylist grew organically and still missed
  vectors (`ast`, `inspect`, `code`, `symtable`, `contextvars`).
  Reviewing every new stdlib addition is not sustainable.
- **Inverting the burden of proof.** A module should be guilty (blocked)
  unless explicitly proven safe and listed, not the other way round.

## Considered Options

1. **Keep the denylist, harden it with more entries.**
2. **Switch to an allowlist** — only explicitly listed modules import.
3. **Drop in-process isolation entirely; require process/container
   isolation now** (layer 5).
4. **Use `sys.audit_hooks` / `sys.settrace` for dynamic interception.**

## Decision Outcome

Chosen option: **Option 2 — allowlist import model**, because it
inverts the burden of proof and makes the policy auditable in one
frozen set. The allowlist is the default-deny boundary; layers 2–4
(network whitelist, resource limits, filesystem isolation) sit behind
it and layer 5 (process isolation) remains the production target.

### Consequences

- **Positive** — a new stdlib module or transitive dependency can no
  longer become an escape vector by omission. The policy is a single
  ~40-entry frozenset that a reviewer can eyeball in 30 seconds.
- **Positive** — the denylist set is *retained* (`DENYLIST_MODULES` in
  [`allowlist.py`](../../engine/plugins/allowlist.py)) as
  defence-in-depth, so a future too-permissive allowlist edit does not
  silently unblock `os`/`subprocess`/`contextvars`. The test suite
  parametrises escape-vector regressions over this set.
- **Negative** — strategy authors who legitimately need a module not in
  the allowlist must request a code change + security review. This adds
  friction, which is the point, but it is real friction.
- **Negative** — CPython bootstraps certain `_`-prefixed C-extension
  modules that cannot be purged from `sys.modules` without crashing the
  interpreter. These are enumerated in
  `_ESSENTIAL_CPYTHON_MODULES` ([`restricted_importer.py:134`](../../engine/plugins/restricted_importer.py))
  and kept reachable; they are harmless C-extensions that cannot be
  used as escape vectors on their own.

## Details

### Enforcement is dual-layered

Source: [`engine/plugins/restricted_importer.py`](../../engine/plugins/restricted_importer.py).

`RestrictedImporter` implements two interception points because a
single hook leaves a gap:

1. **`sys.meta_path` finder** (`find_spec`) — catches `import X` for
   modules not yet loaded. Raises `ImportError` if the root package is
   not in the allowlist.
2. **`builtins.__import__` override** (`_restricted_import`) — catches
   re-imports of modules already cached in `sys.modules`. Without this,
   sandboxed code could do `__import__("os")` and reach the `os` module
   the host process already loaded.

On sandbox startup, `purge_non_allowlisted()` removes every
non-allowlisted entry from `sys.modules` (except CPython essentials),
so the cache itself is clean.

### The `contextvars` replacement

The previous sandbox gated filesystem access on a `ContextVar`
(`_sandbox_active`). A `ContextVar` is process-wide mutable state that
attacker code could clear by importing `contextvars` and resetting it.
The fix was two-fold: `contextvars` is now **denied by the allowlist**
itself, and the gate was replaced with a process-level flag
(`_ProcessSandboxFlag`,
[`sandbox/__init__.py:113`](../../engine/plugins/sandbox/__init__.py))
that is itself unreachable because importing `engine.plugins.sandbox`
is blocked via `_DENIED_SUBMODULES`.

### Builtins curation

Source: [`allowlist.py:333`](../../engine/plugins/allowlist.py) (`CURATED_BUILTINS`).

`CURATED_BUILTINS` exposes a subset of `builtins` to sandboxed code.
Removed: `open`, `eval`, `exec`, `compile`, `__import__`, `globals`,
`locals`, `vars`, `dir`, `getattr` (replaced by a safe wrapper),
`setattr`, `delattr`, `type` (3-arg form), `__build_class__`,
`breakpoint`, `memoryview`. Retained: pure functions (`abs`, `len`,
`sorted`, …), numeric type constructors, and exception classes.

### `__globals__` introspection

A restricted `getattr` blocks access to dunder attributes that lead to
frame/code objects (`__globals__`, `__code__`, `__builtins__`,
`__subclasses__`). This closes the "walk `object.__subclasses__()` to
find a dangerous type" escape vector (gh#912).

## Pros and Cons of the Options

### Option 1 — Denylist (status quo)

- **Pros:** Zero migration cost; no risk of breaking strategies that
  import an obscure-but-safe module.
- **Cons:** Arms-race; structurally cannot be complete; already missed
  vectors in the gh#908 audit.

### Option 2 — Allowlist (chosen)

- **Pros:** Default-deny; auditable; new modules can't slip in by
  omission.
- **Cons:** Breaks strategies importing non-listed modules until the
  allowlist is expanded; needs the CPython-essential carve-out.

### Option 3 — Process/container isolation only

- **Pros:** Strongest boundary; removes the entire in-process escape
  surface.
- **Cons:** Not yet implemented (layer 5). Dropping layers 1–4 now
  would leave **zero** containment until it ships. The allowlist is the
  bridge until process isolation lands.

### Option 4 — `sys.audit_hooks` / `settrace`

- **Pros:** Dynamic, catches runtime escapes not just imports.
- **Cons:** High overhead; `audit_hooks` is CPython 3.8+ but its event
  coverage is incomplete for our needs; `settrace` serialises all
  execution. Both are harder to reason about than a static frozenset.

## Links

- Switch to allowlist: gh#906
- Critical sandbox escape patches: gh#908
- `__globals__` introspection block: gh#912
- Restricted `getattr`: gh#914, gh#916
- Source: [`engine/plugins/allowlist.py`](../../engine/plugins/allowlist.py),
  [`engine/plugins/restricted_importer.py`](../../engine/plugins/restricted_importer.py),
  [`engine/plugins/sandbox/__init__.py`](../../engine/plugins/sandbox/__init__.py)
- Complemented by: [ADR-0010 — Static AST validation + TOCTOU-safe
  strategy loading](0010-static-ast-validation-toctou-loading.md) — the
  parse-time validator and validate-then-exec loader that run *before*
  the runtime hooks in this ADR ever fire
- Related: [`docs/architecture/plugins.md`](../architecture/plugins.md)
