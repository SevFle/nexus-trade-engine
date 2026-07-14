# ADR-0011: Runtime blocking of introspection / sandbox-escape dunders

- **Status**: Accepted
- **Date**: 2026-07-14
- **Deciders**: Lead maintainer + security reviewer
- **Tags**: security, plugins, sandbox

## Context and Problem Statement

[ADR-0007](0007-strategy-sandbox-allowlist-imports.md) closed the
`import` statement and [ADR-0010](0010-static-ast-validation-toctou-loading.md)
closed dynamic-import / code-execution builtins and the loader TOCTOU
window. Both *refer to* code objects — they stop a strategy from *naming*
a forbidden module. They do **not** stop a strategy from *reaching* one
that the host process has already loaded, via the CPython object model.

That is the classic CPython sandbox-escape chain, and it works entirely
with attributes that are present on every object:

- **Type traversal** — `obj.__class__.__base__.__subclasses__()` walks the
  type hierarchy from any innocuous object to *every* class in the running
  interpreter, including `subprocess.Popen` and `os._wrap_close`. From
  there a determined strategy reaches the `os` module the allowlist
  denies — without ever writing the word `import`.
- **Function/closure introspection** — `fn.__globals__` hands over a
  function's module namespace (so a strategy can read a host function's
  globals and pull `os` out of it); `fn.__code__` is a mutable code
  object; `fn.__closure__` / `fn.__func__` unwrap bound methods and
  re-expose all of the above.
- **Namespace access** — `__builtins__` grants `__import__` and every
  builtin; `__dict__` is any namespace.
- **Object-graph reconstruction** — `__reduce__` / `__reduce_ex__` drive
  the pickle protocol and are the canonical entry point for
  deserialisation gadget chains.
- **Module loading** — `__loader__` / `__spec__` / `__objclass__` can
  import/load arbitrary modules.

The layered sandbox in [`engine/plugins/sandbox/__init__.py`](../../engine/plugins/sandbox/__init__.py)
layers 0–4 are all **in-process best-effort**, and the threat-model note
in [`architecture/plugins.md`](../architecture/plugins.md#sandboxing)
already states that only **layer 5** (process/container isolation) is a
real security boundary. So this is **not** an ADR claiming a new
boundary — it records *why* we nonetheless built a runtime introspection
guard on top of the import controls, *what escape classes it narrows*,
and the non-obvious trade-offs the implementation accepts.

Two earlier bypasses forced the design into its current shape and are
worth naming so the constraints make sense:

- **The `contextvars` reset (C-1).** The sandbox's enforcement gate was
  originally a `contextvars.ContextVar` (`_in_sandbox_execution`). A
  `ContextVar` is process-wide mutable state: attacker code can `import
  contextvars`, grab the running context, and reset the flag from inside
  the evaluation, turning the network/filesystem/getattr guards back off
  mid-strategy. The fix was to block `contextvars` from the import
  allowlist **and** replace the gate with a plain process-level flag.
- **The `__init__` stash (C-2).** Restrictions must be active *while the
  strategy object is constructed*, not only during `on_bar`, or the
  strategy can grab a reference to `os`/`builtins` in `__init__` and
  reuse it later. `StrategySandbox.from_factory` exists for this reason.

## Decision Drivers

- **Defence in depth before layer 5 lands.** Layer 5 (separate process
  per strategy) is the production target but is not built. Until it is,
  narrowing the in-process attack surface is worthwhile *as long as it
  is honestly labelled best-effort*.
- **`getattr` is the one builtin we can hook from pure Python.** Direct
  dotted access (`obj.__class__`) cannot be intercepted without a custom
  object model. So any guard we build can only ever cover the
  *dynamic* `getattr(obj, name)` form — which is exactly the form
  introspection helpers and attacker gadget code use. Hooking it
  therefore costs little and raises the bar, even though it is incomplete
  by construction.
- **Keep legitimate introspection working.** `inspect`, `dataclasses`,
  and `pydantic` all call `getattr(obj, '__globals__', None)` and
  similar internally. A guard that *raised* on every blocked-attribute
  probe would break these libraries inside the sandbox, so the
  three-argument `getattr` contract must be honoured.
- **No new module-level escape surface.** Whatever we add to
  `engine/plugins/sandbox/__init__.py` runs in the same address space as
  attacker code. The guard module itself must not become a treasure map
  of dangerous module references.

## Considered Options

1. **Do nothing in-process; rely solely on layer 5.** Ship no
   runtime introspection guard; document that type traversal is
   permitted until process isolation lands.
2. **Static AST blocking of dotted access** (forbid `obj.__class__` etc.
   at parse time, like ADR-0010 forbids `exec`).
3. **Custom object model / restricted interpreter** (e.g. rewrite
   strategy code to proxy every object through a guard).
4. **Runtime `builtins.getattr` hook + blocked-attribute set + a
   process-level (not contextvar) gate, with no module-level dangerous
   references** (chosen).

## Decision Outcome

Chosen option: **Option 4**, because it narrows the dynamic-`getattr`
escape class for negligible cost and zero legitimate-library breakage,
while remaining honestly best-effort and layered beneath the eventual
layer-5 boundary. Concretely, four mechanisms ship together in
[`sandbox/__init__.py`](../../engine/plugins/sandbox/__init__.py):

1. **`_BLOCKED_ATTRS` — a frozen set of sandbox-escape dunders.**
   `_restricted_getattr` (installed over `builtins.getattr` during an
   evaluation) refuses any name in the set. The set is grouped by escape
   class (type traversal, function/closure introspection, namespace /
   builtins access, pickle gadget chains, module loading, subclass
   hooks). It grew incrementally across #1448 (`__class__`, `__base__`,
   `__builtins__`, …) and #1450 (`__dict__`, `__reduce__`,
   `__reduce_ex__`, `__wrapped__`, `__self__`, `__loader__`, `__spec__`,
   `__objclass__`, `__defaults__`, `__kwdefaults__`) as each step of the
   documented escape chain was enumerated.
2. **`_RestrictedObject` over `builtins.object`.** Replaces
   `builtins.object` for the duration of an evaluation so that the
   classic `object.__subclasses__()` entry point raises
   `RuntimeError`. This is the most common first hop of a type-traversal
   gadget, so it gets its own guard rather than relying on the caller to
   route it through `getattr`.
3. **`_ProcessSandboxFlag` — the non-contextvar gate.** Replaces the
   `ContextVar` that suffered the C-1 reset. It is a plain boolean with
   a minimal `get`/`set`/`reset` API, and it is unreachable from strategy
   code because importing `engine.plugins.sandbox` is denied by the
   restricted importer's `_DENIED_SUBMODULES`. (In the layer-5
   architecture this flag becomes implicit: the child process *is* the
   sandbox.)
4. **The "no module-level dangerous references" discipline.** The module
   deliberately keeps **no** module-level binding to `os`, `io`,
   `shutil`, `httpx`, `resource`, `asyncio`, or `builtins`. They are
   imported *inside* the methods that need them, while restrictions are
   off. A handful of captured *callables/values* (`_realpath`, `_sep`,
   `_eval_lock`, `_wait_for`, `_iscoroutine`) are retained because the
   guarded code paths need them while restrictions are *on* — none of
   them exposes a dangerous module object.

### Consequences

- **Positive** — the dynamic `getattr(obj, '__globals__')` escape
  (and every other member of `_BLOCKED_ATTRS`) is refused at runtime,
  with a violation recorded in `sandbox.metrics.errors` / `last_error`
  even when the strategy swallows the exception.
- **Positive** — `inspect`, `dataclasses`, and `pydantic` keep working
  inside the sandbox: their three-argument `getattr(obj, name, default)`
  probes receive the caller's *default* (never the blocked value, never
  an exception), so the attribute's contents stay unreachable without
  breaking introspection libraries.
- **Positive** — the C-1 `contextvars` reset is structurally closed:
  the module that could manipulate the gate is blocked by the import
  allowlist, and the gate is no longer a `ContextVar` anyway.
- **Negative — and stated plainly — this is still not a security
  boundary.** Direct dotted access (`obj.__class__`) cannot be hooked
  from pure Python, so it is governed only by the (not-yet-built) layer
  5 process boundary. Anyone relying on this guard for untrusted code is
  relying on the wrong layer. The class docstring and the threat-model
  note in [`plugins.md`](../architecture/plugins.md#sandboxing) both
  say so.
- **Negative** — the guard installs and restores global builtins
  (`builtins.getattr`, `builtins.open`, `builtins.object`,
  `io.open`, `httpx.AsyncClient.send`) around each evaluation. Two
  consequences follow: evaluations are **serialised** via a process-wide
  `_eval_lock` so concurrent sandboxes cannot corrupt each other's
  builtin patches (the C-4 class), and every restore is ownership-checked
  (`builtins.getattr is self._restricted_getattr`) so out-of-order
  teardown cannot clobber an importer stacked on top of us.

## Details

### The three-argument `getattr` contract

`_restricted_getattr` honours Python's `getattr(obj, name, default)`
semantics precisely:

| Call form | Blocked name | Non-blocked name |
|---|---|---|
| `getattr(obj, name)` (2-arg) | `PermissionError` | real value |
| `getattr(obj, name, default)` (3-arg) | caller's `default` | real value |

Returning the caller's default for a blocked name (rather than the real
value, rather than raising) is what keeps `inspect.get_annotations` and
`_signature_from_function` — which both call
`getattr(obj, '__globals__', None)` — functional. The blocked attribute's
*contents* are never returned either way.

### Metrics accounting without double counts

A blocked-attribute attempt is recorded in `metrics.errors` /
`metrics.last_error` *inside* `_restricted_getattr`, before it raises or
returns the default — so a strategy that swallows the `PermissionError`
cannot hide the attempt. To avoid counting the same violation twice when
the `PermissionError` propagates up to `_evaluate_inner`, the exception
is tagged with `err._sandbox_violation_counted = True`, and the
per-evaluation `_getattr_violation_counted` flag is reset on every
`_evaluate_inner` entry. The tag is bound to the *exception object*
(rather than the instance flag) deliberately: if the strategy swallows
the `PermissionError` and then raises a *different* error in the same
evaluation, that later error must still be counted — an instance flag
would mask it, an exception tag does not.

### What the guard does *not* cover

- **Direct dotted access** (`obj.__class__`, `obj.__subclasses__()`)
  except `object.__subclasses__()`, which the `_RestrictedObject` swap
  covers. Everything else is layer 5's job.
- **Attribute access via `operator.attrgetter` / `getattr` re-exports**
  that are themselves allowlisted. (`operator` is not in the allowlist,
  so this is currently moot, but the guarantee is only as strong as the
  import policy.)
- **C-level introspection** (`ctypes`, the `gc` module's object graph).
  `ctypes` is denied by the allowlist; `gc` is in the bootstrap set and
  harmless without a way to *call* into it, but this is defence in
  depth, not a proof.

## Pros and Cons of the Options

### Option 1 — Do nothing in-process; rely on layer 5

- **Pros:** Simplest; zero in-process monkeypatching and zero serialisation
  lock; honest about the boundary.
- **Cons:** Until layer 5 lands, every accidental `getattr(obj,
  '__globals__')` in a *buggy* (not malicious) strategy silently leaks
  host state; the dynamic-`getattr` escape is free for the taking. We
  would be shipping a known trivial escape with no speed bump.

### Option 2 — Static AST blocking of dotted access

- **Pros:** Fails fast at parse time, side-effect free, in the spirit of
  ADR-0010.
- **Cons:** Cannot be made complete in general. `obj.__getattribute__`
  ("__class__")`, `getattr` indirection through data structures, and
  attribute names constructed at runtime (`getattr(obj, chr(95)*2 +
  'class__')`) all evade a static checker. A partial AST rule gives a
  false sense of security; a complete one does not exist. Rejected.

### Option 3 — Custom object model / restricted interpreter

- **Pros:** The only option that can intercept *direct* dotted access.
- **Cons:** Enormous implementation cost (rewrite every object access, or
  adopt a restricted interpreter like Ren'Py's / a WASI target). Duplicates
  the eventual layer-5 boundary and would be thrown away. Premature by
  the same logic ADR-0010 used to reject a loader subprocess.

### Option 4 — Runtime `getattr` hook + blocked set + process flag (chosen)

- **Pros:** Cheap; narrows the dynamic-`getattr` class that real
  introspection gadget code actually uses; honours the 3-arg `getattr`
  contract so libraries keep working; structurally closes the C-1
  contextvar reset; honest about its own limits.
- **Cons:** Incomplete by construction (dotted access); requires
  serialised evaluations and global-builtin patching with careful
  ownership-checked teardown; the blocked set is a growing denylist, so
  it needs the same vigilance the import *denylist* needs (mitigated
  because the import *allowlist* is the primary control).

## Links

- Block `__globals__` introspection via restricted getattr: commit
  `bd0b17a`
- Block dangerous dunder attributes (`__class__`, `__base__`,
  `__builtins__`, …): gh#1448
- Block `__dict__`, `__reduce__`, `__loader__`, `__spec__`, …
  (introspection / pickle / module-loading escape): gh#1450
- Document the security-bypass fixes: gh#1446
- Source:
  [`engine/plugins/sandbox/__init__.py`](../../engine/plugins/sandbox/__init__.py)
  (`_BLOCKED_ATTRS`, `_restricted_getattr`, `_RestrictedObject`,
  `_ProcessSandboxFlag`, `StrategySandbox.from_factory`)
- Tests:
  [`tests/test_sandbox_restricted_getattr.py`](../../tests/test_sandbox_restricted_getattr.py),
  [`tests/test_sandbox_blocked_attrs.py`](../../tests/test_sandbox_blocked_attrs.py),
  [`tests/test_sandbox_context_var.py`](../../tests/test_sandbox_context_var.py)
- Builds on: [ADR-0007](0007-strategy-sandbox-allowlist-imports.md)
  (runtime import allowlist) and
  [ADR-0010](0010-static-ast-validation-toctou-loading.md) (static AST
  validation + TOCTOU-safe loading). Both remain Accepted; this ADR is
  the introspection-facing companion ring.
- Related: [`architecture/plugins.md`](../architecture/plugins.md#sandboxing)
  (threat model and the layer table)
