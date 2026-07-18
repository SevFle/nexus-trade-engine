# ADR-0012: Shared `SafeIdentifier` validation for path parameters

- **Status**: Accepted
- **Date**: 2026-07-18
- **Deciders**: Lead maintainer + security reviewer
- **Tags**: security, api, validation

## Context and Problem Statement

Several REST routes take a *user-controlled identifier* as a path
parameter — most notably `strategy_id` on every
`/api/v1/strategies/{strategy_id}/...` route and `strategy_name` on
every `/api/v1/scoring/{strategy_name}/...` route. Those identifiers
then flow into four hostile-input-sensitive sinks:

1. **Registry lookups** — `app.state.plugin_registry.get(strategy_id)`
   and the scoring `PluginRegistry(...)` constructor key off the raw
   string.
2. **Database queries** — the scoring results handler issues
   `select(ScoringSnapshot).where(strategy_id == ...)` against the
   value.
3. **Log lines** — structlog emits the identifier verbatim in the
   request-scoped context.
4. **Reflected error `detail` strings** — `404` bodies carry the raw
   identifier: `f"Strategy '{strategy_id}' not found"`.

Without a validation gate at the framework layer, every one of those
sinks is on its own to defend against markup injection
(`'><svg onload=…>`), path traversal (`../`), SQL/log-forging
sequences (`'; DROP TABLE--`), control characters, and Unicode
look-alikes. Per-handler discipline cannot hold: a new route added
six months later silently inherits no defense, and there is no
compile-time signal that the author forgot one.

Two commits closed this hole. `a6b6fd24` added a per-route
`Annotated[str, Path(pattern=…)]`; `ef25466f` factored the pattern
and the length cap out of the route modules into a single
[`engine.api/validators.SafeIdentifier`](../../engine/api/validators.py)
alias so the contract cannot drift between routes. This ADR records
*why* the validation lives at the FastAPI layer (and not in the
handlers), *why* the regex is shaped the way it is (pydantic-v2 / Rust
`regex` crate constraints), and the limits of the approach.

## Decision Drivers

- **Reject once, defend everywhere.** A pattern enforced by FastAPI's
  request-validation layer runs *before* the handler body. A hostile
  identifier therefore never reaches the registry, the DB, structlog,
  or a reflected `detail` — it is rejected with HTTP `422` at the
  boundary. One chokepoint beats four per-sink checks.
- **The contract must be unit-testable in isolation.** Per-route
  `Annotated[...]` literals duplicated the pattern in every module and
  could only be exercised through an ASGI client. A shared alias is
  testable directly against the regex, and the regression suite
  ([`tests/test_identifier_validation_sev.py`](../../tests/test_identifier_validation_sev.py))
  parametrizes one `_HOSTILE_IDENTIFIERS` list across every
  identifier-bearing route so a new route opts into the test for free
  by adopting the alias.
- **Pydantic v2 compiles patterns with the Rust `regex` crate.** That
  crate does **not** support lookahead / look-behind assertions. Any
  pattern that depends on look-around raises `regex parse error` at
  app-import time, so the dot-discipline contract has to be expressed
  constructively.
- **Dots are legitimate but only as separators.** Plugin / strategy
  identifiers namespace with dots (`vendor.meanreversion.v2`), so a
  blanket "no dots" rule would reject real values. The contract is
  specifically "dots between non-empty, dot-free tokens" — no
  leading/trailing dot, no `..`.

## Considered Options

1. **Do nothing at the framework layer; rely on per-handler sanitization
   or per-sink parameterization.** Defense-in-depth at each sink.
2. **Per-route `Annotated[str, Path(pattern=…)]` literals** (the
   intermediate state in `a6b6fd24`, before the refactor).
3. **A centralised input-sanitization middleware** that rewrites /
   rejects identifiers in the request path before routing.
4. **A single shared `SafeIdentifier` type alias bundling the pattern
   and length cap, used as the path-parameter type** (chosen).

## Decision Outcome

Chosen option: **Option 4**, shipped as
[`engine/api/validators.SafeIdentifier`](../../engine/api/validators.py):

```python
MAX_IDENTIFIER_LENGTH: int = 64
SAFE_IDENTIFIER_PATTERN: str = r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$"

SafeIdentifier = Annotated[
    str,
    Path(
        pattern=SAFE_IDENTIFIER_PATTERN,
        max_length=MAX_IDENTIFIER_LENGTH,
    ),
]
```

Adoption today is deliberate and narrow: it covers exactly the routes
that take a *plugin/strategy* identifier as a path parameter and feed
it into registry/DB/log/reflection sinks —

- `GET/POST /api/v1/strategies/{strategy_id}` and the
  `activate` / `deactivate` / `reload` / `health` sub-routes
  ([`routes/strategies.py`](../../engine/api/routes/strategies.py));
- `POST /api/v1/scoring/{strategy_name}/run` and
  `GET /api/v1/scoring/{strategy_name}/results`
  ([`routes/scoring.py`](../../engine/api/routes/scoring.py)).

Other path parameters (`portfolio_id`, `webhook_id`, `backtest_id`,
`task_id`, document slugs under `/legal`, OAuth `provider`) are **not**
covered by this alias. They are UUIDs, numeric IDs, or values drawn
from a small fixed enumeration, and they are validated by their
existing type annotations or in-handler membership checks.

### Why this regex, exactly

The pattern `^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$` is shaped by three
constraints that interact:

1. **Anchored end-to-end.** `^...$` makes the contract independent of
   whether the validator internally uses `re.match`, `re.search`, or
   `re.fullmatch`. FastAPI / pydantic versions differ; the anchors make
   drift impossible.
2. **No look-around.** Pydantic v2 compiles the pattern with the Rust
   `regex` crate, which rejects lookahead/look-behind at parse time.
   The dot discipline is therefore expressed *constructively* — "one
   or more dot-free tokens separated by *single* dots" — rather than
   as "any character except a leading/trailing/consecutive dot":
   - `[A-Za-z0-9_-]+` — the first token requires ≥1 dot-free char, so
     the identifier can neither be empty nor *start* with a dot.
   - `(\.[A-Za-z0-9_-]+)*` — zero or more `.` separators, each of
     which must be followed by ≥1 dot-free char, so a trailing dot, a
     leading dot in a later segment, and `..` are all structurally
     impossible.
3. **64-char cap.** A hostile or runaway identifier cannot blow up a
   log line, a DB index, or a reflected error `detail`. `64` matches
   the common "short identifier" ceiling (e.g. the `strategy.id`
   field in plugin manifests) and is mirrored in the named constant
   `MAX_IDENTIFIER_LENGTH` so tests and callers reference the same
   value if the cap ever moves.

### Consequences

- **Positive** — hostile identifiers (`'><svg onload=alert(1)>`,
  `strategy" onerror=…`, `'; DROP TABLE--`, `has space`, `stratégie`,
  `a*b`, `a@b`, …) are rejected with `422` *before* the handler runs,
  on every covered route. The regression test pins this for each route
  individually.
- **Positive** — the contract is one definition. A new identifier
  route gets full validation by writing
  `strategy_id: SafeIdentifier` instead of re-deriving (and possibly
  diverging from) the pattern.
- **Positive** — the pattern is unit-testable in isolation; the
  regression suite does not have to stand up a router per route.
- **Negative** — FastAPI's framework-generated `422` body *echoes the
  invalid input value* in its `detail` payload. The
  [`TestNoHostileEcho`](../../tests/test_identifier_validation_sev.py)
  class pins that the dangerous substrings are neutralised by JSON
  serialisation, but the decoded hostile payload may still appear
  verbatim in the framework's error detail. Any admin UI that renders
  422 bodies verbatim must HTML-escape them; this is a known limit,
  not something the validator can close on its own.
- **Negative** — `.` is allowed, which means the pattern admits very
  long dotted chains up to 64 chars. That is intentional (namespaced
  plugin IDs) but means the alias is *not* a substitute for
  rate-limiting or authz on routes that accept it.

## Pros and Cons of the Options

### Option 1 — Do nothing; defend per sink

- **Pros:** No new abstraction; each sink keeps its existing input
  contract (parameterised queries, structlog redaction, HTML-escaping
  in the UI).
- **Cons:** No compile-time signal when a new route forgets the
  defense; four sinks to audit forever; the reflected `detail` string
  in the `404` body is undefended by construction. Rejected: defence
  in depth is good, but it should not be the *only* layer for a
  four-sink identifier.

### Option 2 — Per-route `Annotated[...]` literals

- **Pros:** Minimal change; no new module.
- **Cons:** Duplicates the pattern in every route module and lets the
  copies drift (one route loosens the regex in a hot-fix and the
  contract silently splits). The pattern itself is non-obvious enough
  (the no-look-around formulation) that centralising it is worth the
  indirection. This is the state `a6b6fd24` shipped in;
  `ef25466f` retired it.

### Option 3 — Centralised input-sanitization middleware

- **Pros:** One place; covers routes that haven't been written yet.
- **Cons:** A middleware that rewrites or rejects path parameters has
  to understand routing semantics (which segment is an identifier vs.
  a fixed literal like `/activate`), and Starlette decodes `%2F` into
  a path separator *before* the route is matched — so a middleware
  cannot reliably see the raw identifier the way the
  path-parameter validator can. It also conflates *validation* (a
  routing-layer concern) with *sanitization* (a sink-layer concern),
  which makes the security boundary harder to reason about. Rejected.

### Option 4 — Shared `SafeIdentifier` alias (chosen)

- **Pros:** One regex, one length cap, one chokepoint, unit-testable
  in isolation; new routes adopt it by writing a type annotation; the
  contract is identical across every route by construction.
- **Cons:** Adoption is opt-in per route, so it is only as good as the
  discipline of "use the alias when adding an identifier route." That
  discipline is reinforced by the parametrised regression test (a new
  identifier route that forgets the alias won't have a corresponding
  `test_*_rejects_malformed_id` row, which is visible in code review)
  and by the docstrings at the top of `routes/strategies.py` and
  `routes/scoring.py`.

## Links

- Add regex validation to identifier parameters: commit `a6b6fd24`
- Extract validation logic into `validators` module: commit `ef25466f`
- Source: [`engine/api/validators.py`](../../engine/api/validators.py)
  (`SafeIdentifier`, `SAFE_IDENTIFIER_PATTERN`, `MAX_IDENTIFIER_LENGTH`)
- Tests:
  [`tests/test_identifier_validation_sev.py`](../../tests/test_identifier_validation_sev.py)
  (per-route 422 regression, hostile-payload echo assertion)
- Related: [`api-reference.md`](../api-reference.md) (identifier
  validation subsection under "Errors") and
  [`architecture/plugins.md`](../architecture/plugins.md) (the
  plugin-id surface this protects)
- Companion ring to the sandbox ADRs
  ([0007](0007-strategy-sandbox-allowlist-imports.md),
  [0010](0010-static-ast-validation-toctou-loading.md),
  [0011](0011-runtime-introspection-blocking.md)): those defend the
  *code* a plugin can run; this ADR defends the *name* a caller can
  hand the registry that loads it.
