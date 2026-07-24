<!--
  Companion to known-limitations.md. These five items share one shape:
  a security / real-time surface is only *partially* wired into the runtime
  path — either a stricter reference implementation exists on disk but is not
  registered (LDAP, OIDC, the sandbox AST validator), or a real-time surface
  is incomplete (the per-process WebSocket connection registry and its
  JWT-only authenticator). Pulled into its own page so the main
  known-limitations.md stays under the 500-line doc limit.

  Relative links are one level deeper here (docs/known-limitations/) than on
  the root index, so engine sources are ../../engine and cross-doc links are
  ../adr, ../architecture.
-->

# Auth, Sandbox & Real-Time — Partially-Wired Surfaces

Companion to [`known-limitations.md`](../known-limitations.md). The
items below are the most code-link-heavy entries from the limitations
inventory: every one is a surface that *exists* on disk but is not
fully wired into the request path. Read the main
[`known-limitations.md`](../known-limitations.md) first for the P0
data-loss / persistence issues — those are higher priority than
anything here.


<a id="ldap-has-no-route"></a>
## P1 — LDAP is registered but has no route

**Where**:
[`engine/app.py`](../../engine/app.py) (`_build_auth_registry`, the
`case "ldap"` branch),
[`engine/api/auth/ldap.py`](../../engine/api/auth/ldap.py) (the
`python-ldap`-backed provider wired into the registry), and
[`engine/auth/providers/ldap.py`](../../engine/auth/providers/ldap.py)
(a newer, more robust `ldap3`-backed provider landed in PR #1368 that
is *not* wired).

`NEXUS_AUTH_PROVIDERS=…,ldap` makes `create_app()` register an
`LDAPAuthProvider` in the `AuthProviderRegistry`, so a caller that
imports the registry can drive it via
`registry.authenticate("ldap", username=…, password=…, db=…)`. But
**no HTTP route does so**:

- `POST /api/v1/auth/login` hard-codes `"local"`
  ([`engine/api/routes/auth.py`](../../engine/api/routes/auth.py)).
- `GET /api/v1/auth/{provider}/callback` is OAuth-shaped — it expects a
  `code` and validates an OAuth state cookie, neither of which fits
  LDAP's username/password flow.

Consequence: an operator who enables LDAP can prove the bind works
from a Python shell, but no end user can log in through the engine.

There is also a second, more sophisticated LDAP implementation now:
`engine/auth/providers/ldap.py` (PR #1368, 662 lines — search-then-bind,
an `LDAPConnectionPool` with single-flight safety, typed exceptions
inheriting from the shared `OAuthError`, lazy `ldap3` import). It is
exported from `engine.auth.providers` but **not** registered in the
app — it is library-only, paralleling the wired `engine/api/auth/ldap.py`.

**Workaround today**: none at runtime over HTTP. Treat LDAP as a
library-callable provider; do not list it in `NEXUS_AUTH_PROVIDERS`
expecting end users to authenticate against it. Operators who need
LDAP today must add a route that calls
`registry.authenticate("ldap", …)` and mints tokens via the existing
`_mint_tokens` / `_store_refresh_token` helpers.

**Fix path**: (1) add `POST /api/v1/auth/ldap/login` (body
`{username, password}`) that drives the registry's `ldap` provider;
(2) decide whether to keep the simpler `engine/api/auth/ldap.py`
(`python-ldap`) or replace it with the newer
`engine/auth/providers/ldap.py` (`ldap3`, pooled, search-then-bind)
and rewire `_build_auth_registry`'s `case "ldap"` branch to the new
one; (3) extend the role-mapping documentation in
[`adr/0002-auth-rbac.md`](../adr/0002-auth-rbac.md) once the wiring
choice is made.

---

<a id="oidc-two-implementations"></a>
## P1 — OIDC has two implementations; only the discovery-based one is wired

**Where**: [`engine/api/auth/oidc.py`](../../engine/api/auth/oidc.py)
(wired) vs [`engine/auth/oidc.py`](../../engine/auth/oidc.py) (PR #1633,
library-only)

There are **two OIDC providers** on disk, and they are not the same
code path:

- **`OIDCAuthProvider`** in `engine/api/auth/oidc.py` is the one
  `create_app()._build_auth_registry()` actually registers
  (`case "oidc"` in [`engine/app.py`](../../engine/app.py)). It is
  discovery-document driven: it fetches `oidc_discovery_url`, reads
  `token_endpoint` / `jwks_uri` / `authorization_endpoint` from it, and
  creates/upserts the `User` row (`auth_provider="oidc"`). This is the
  provider an operator reaches by setting
  `NEXUS_AUTH_PROVIDERS=…,oidc` and hitting
  `GET /api/v1/auth/oidc/callback`.
- **`OIDCProvider`** in `engine/auth/oidc.py` (PR #1633) is a generic,
  issuer-configurable OIDC client implementing the
  `IOAuthProvider` contract (`engine/auth/base.py`). It is more
  rigorous than the wired adapter — JWKS caching with `force=` refresh,
  an injectable `_JWKSClient`/`httpx` transport for tests, an explicit
  signing-algorithm allowlist that makes `alg=none` impossible, HTTPS
  enforcement on JWKS/token endpoints (localhost exempt), PKCE
  `code_verifier` forwarding, and a typed exception hierarchy
  (`OIDCError` / `InvalidTokenError` / `TokenExchangeError` /
  `DiscoveryError`). It is configurable via the **separate**
  `oidc_issuer` / `oidc_jwks_uri` settings (not `oidc_discovery_url`)
  and can be built by `engine.auth.get_oauth_provider("oidc")` — but
  **that factory is not on the request path**, so this provider is
  library-only today.

This is the same shape as the [LDAP split](#ldap-has-no-route) and the
Google/GitHub split recorded in
[ADR-0002](../adr/0002-auth-rbac.md#evolution--how-this-actually-landed):
the `engine/auth/` tree is a standalone protocol library whose providers
are not wired into the runtime registry.

**Workaround today**: the wired `OIDCAuthProvider` works end-to-end for
real users — use it. Treat `engine/auth/oidc.OIDCProvider` as a
library/reusable component (or as the stricter reference implementation)
until the two are reconciled. Do not assume configuring `oidc_issuer`
will change login behaviour; login is driven by `oidc_discovery_url`.

**Fix path**: (1) decide whether the JWKS-verify strictness of
`engine/auth/oidc.py` (alg allowlist, HTTPS enforcement, typed
errors) should replace the inline verification in
`engine/api/auth/oidc.py`; (2) if so, have the wired adapter delegate
verification to `OIDCProvider.verify_id_token` and collapse the two
config surfaces (`oidc_discovery_url` vs `oidc_issuer`/`oidc_jwks_uri`);
(3) record the choice in
[ADR-0002](../adr/0002-auth-rbac.md#evolution--how-this-actually-landed).

---

<a id="ast-validator-two-implementations"></a>
## P1 — Static AST validator has two implementations; only the simpler one is wired

**Where**: [`engine/plugins/restricted_importer.py`](../../engine/plugins/restricted_importer.py)
(wired) vs [`engine/plugins/sandbox/ast_validator.py`](../../engine/plugins/sandbox/ast_validator.py)
(PR #1647, patched #1653 — library-only)

This is the import-checking analogue of the
[OIDC split](#oidc-two-implementations) and
[LDAP split](#ldap-has-no-route): there are **two** static, parse-time
AST validators on disk, and the one actually on the plugin-load path is
the simpler of the two.

- **`ImportValidator`** in `engine/plugins/restricted_importer.py` is
  the **wired** one. It is invoked at plugin load by
  [`engine/plugins/registry.py`](../../engine/plugins/registry.py)
  (`ImportValidator(DENYLIST_MODULES).validate(source_bytes)`, run
  **before** `compile`/`exec`) — this is the Layer 0 check recorded in
  [ADR-0010](../adr/0010-static-ast-validation-toctou-loading.md) and
  [`architecture/plugins.md`](../architecture/plugins.md). Its
  `visit_ImportFrom` flags only `from <blocked> import …` where the
  **module root** is on the denylist; it **skips relative imports**
  (`level > 0` with no absolute module) and does **not** reject wildcard
  `from … import *`. It returns a flat `list[str]` and applies the
  denylist only — no parse-time allowlist enforcement (that lives in the
  runtime Layer 1 hook).
- **`ASTValidator`** in `engine/plugins/sandbox/ast_validator.py`
  (PR #1647, patched #1653) is the stricter successor. It additionally:
  - rejects **wildcard** `from … import *` outright — relative *and*
    absolute — as `CODE_FORBIDDEN_FROM_IMPORT`, because the bound names
    cannot be enumerated statically (the #1653 *"critical security
    bypass for `from . import *`"* lived here);
  - flags **relative imports that escape the strategy package**
    (`level > 1`) as `CODE_RELATIVE_IMPORT`, while checking the module
    root consistently for within-package (`level == 1`) relative
    imports;
  - enforces an **allowlist with denylist precedence** at parse time
    (defence-in-depth that catches unlisted modules before `exec`, not
    only at import time);
  - returns a **structured, total** `ValidationResult` of
    `Violation(line, col, code, severity, …)` records instead of a flat
    `list[str]`, capturing `SyntaxError` as a violation rather than
    raising.

  It is **not imported anywhere in the engine** (non-test) code today —
  confirmed by grep across `engine/` — so none of these stricter checks
  are live. It is a reusable component / reference implementation
  awaiting wiring.

**Framing on severity (read this before assuming an exploit):** the
wired `ImportValidator` not blocking `from … import *` is a
**defence-in-depth gap at Layer 0**, *not* a full sandbox escape. Every
`from … import` still passes through the **runtime** allowlist
([`RestrictedImporter`](../../engine/plugins/restricted_importer.py),
Layer 1) when the names are actually bound, so a forbidden module is
still refused at exec time. The gap is that the *static* pre-`exec`
trip-wire is weaker than the stricter `ASTValidator` was built to be, so
an attempt that should have died at parse time instead survives to the
runtime hook.

**Workaround today**: assume Layer 0 statically rejects only
absolute/denylisted imports and forbidden *calls*
(`exec`/`eval`/`compile`/`__import__`/`importlib.import_module`); rely
on the runtime Layer 1 allowlist as the real import gate. Treat
`ASTValidator` as the stricter reference and do **not** assume wildcard
or escaping-relative rejection is enforced at parse time.

**Fix path**: (1) switch [`registry.py`](../../engine/plugins/registry.py)
from `ImportValidator(...).validate(...)` to
`validate_strategy_source(...)` (or `ASTValidator(...).validate(...)`),
adapting the `list[str]`→`ValidationResult` boundary so existing
violation handling keeps working; (2) confirm the new wildcard /
relative-import / allowlist checks pass the `tests/test_ast_validator.py`
corpus (allowlist/denylist/forbidden-call/relative-import cases added in
#1647) before flipping it on; (3) record the wiring decision in
[ADR-0010](../adr/0010-static-ast-validation-toctou-loading.md) and retire
`ImportValidator` (or fold it in) once nothing else references it.

---

## P2 — WebSocket connection registry is process-local (events are cross-replica)

**Where**: [`engine/api/ws/connection_manager.py`](../../engine/api/ws/connection_manager.py).

The live `WebSocket` objects themselves live in a per-process dict, so a
client must reconnect to the replica it originally hit. **Event delivery is
already cross-replica**, however: the
[`EventBusBridge`](../../engine/api/ws/event_bridge.py) subscribes to the
[`EventBus`](../../engine/events/bus.py), which publishes over Redis/Valkey
pub/sub, so events emitted on replica A reach local connections on every
replica.

The remaining gap is that there is no shared connection registry or sticky
sessioning, so a client whose replica dies must reconnect. There is also no
back-pressure signal back to the `EventBus` if a room has no local
subscribers — the bridge fans out unconditionally.

**Workaround today**: deploy behind a load balancer that supports
connection draining, or accept that a replica restart drops its in-flight WS
sessions. Event correctness (via the bridge) does not depend on a single
replica.

---

## P2 — WebSocket does not accept API keys

**Where**: [`engine/api/ws/auth.py`](../../engine/api/ws/auth.py#L158)

The active WS authenticator calls `decode_token` (JWT only). It does
**not** run the `is_engine_token` / `find_active_by_token` path that the
REST `get_current_user` dependency uses, so a `nxs_*` API key cannot open
a WS connection. The legacy `routes/websocket.py` did support API keys;
that code is no longer mounted.

**Workaround today**: headless clients mint a short-lived JWT via
`POST /api/v1/auth/login` (or the API-key → JWT exchange if added) and
use that for WS. If long-lived WS access for automation is needed, port
the API-key branch from the legacy endpoint into `ws/auth.py`.

---
