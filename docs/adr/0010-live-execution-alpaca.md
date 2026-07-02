# ADR-0010: Live execution backend — Alpaca-compatible REST with injectable transport

- **Status**: Accepted
- **Date**: 2026-06-30
- **Deciders**: Execution reviewer + live-trading reviewer
- **Tags**: execution, live-trading, brokers, testing, reliability

## Context and Problem Statement

The engine already had two `ExecutionBackend` implementations:
backtest (simulated fills) and paper
([`engine/core/execution/paper.py`](../../engine/core/execution/paper.py)).
A third — `engine/core/execution/live.py:LiveBackend` — existed only
as a **scaffold**: it tracked connection state but talked to no broker
and validated no credentials, so a misconfiguration could never fail
loudly and no real order could ever leave the process.

To make live trading reachable we needed a concrete adapter that:

1. Submits, cancels, and polls orders against a real broker.
2. Fails closed on bad credentials **before** any order is risked.
3. Maps transport/HTTP failures into the broker error hierarchy the
   order manager and live loop already react to
   (`BrokerAuthError` / `BrokerConnectionError` / `BrokerRejectError`).
4. Is unit-testable without touching the network.
5. Cannot, by default, route a real-money order even if an operator
   fat-fingers the config.

## Decision Drivers

- **Same ABC as backtest/paper.** The order manager calls one
  `execute()` contract regardless of mode. Adding live must not
  fork the OMS pipeline.
- **Testability parity.** The data provider (`AlpacaDataProvider`) and
  broker adapter ([`engine/core/brokers/alpaca.py`](../../engine/core/brokers/alpaca.py))
  already inject `httpx.AsyncClient` so tests run on `MockTransport`.
  The execution backend must follow the same pattern — a network call
  in a unit test is a bug.
- **Duplicate-order safety.** Order submission is non-idempotent. A
  transport blip between sending a `POST /v2/orders` and reading the
  response leaves the engine unable to tell whether the broker placed
  the order. Retrying blindly double-places it.
- **Fail-closed by default.** Operators configure credentials per
  environment. A live adapter that silently defaulted to real money
  would be dangerous; the safe default must be paper.

## Considered Options

1. **Promote the existing scaffold** (`engine/core/execution/live.py`)
   by wiring the broker client directly into it.
2. **A new concrete class behind a vendor SDK** (e.g. the official
   `alpaca-py`) wrapping `submit/cancel/get`.
3. **A new concrete class speaking Alpaca-compatible REST over an
   injectable `httpx.AsyncClient`** — separate from the scaffold.

## Decision Outcome

Chosen option: **Option 3** — a new
[`LiveExecutionBackend`](../../engine/execution/live_backend.py) in a
new package (`engine/execution/`), speaking Alpaca-compatible REST over
an injectable `httpx.AsyncClient`, separate from the scaffold. The
scaffold stays put so the factory's `live` name and existing imports are
undisturbed; the new class is the concrete write path.

### How it works

`LiveExecutionBackend` ([`engine/execution/live_backend.py`](../../engine/execution/live_backend.py))
exposes two surfaces:

- **ABC surface** — `connect` / `disconnect` / `execute`, so the OMS
  treats it identically to backtest/paper. `connect()` validates
  credentials + reachability via `GET /v2/account` and records the
  monotonic connect time; `execute()` is built on `submit_order` and
  translates every broker error into a structured `FillResult` failure
  rather than propagating, so the OMS loop never dies on a bad order.
- **Broker-direct async helpers** — `submit_order`
  (`POST /v2/orders`), `cancel_order` (`DELETE /v2/orders/{id}`),
  `get_order_status` (`GET /v2/orders/{id}`). Components that need
  finer control than `execute()` (status polling, explicit cancel) call
  these directly.

The three design levers that earned the ADR:

1. **Injectable transport.** `client=` is optional; when omitted a real
   `httpx.AsyncClient` is created lazily on first use, so construction
   is cheap and side-effect free. Tests pass a `MockTransport`-backed
   client and never touch the network — exactly the
   `AlpacaDataProvider` / `brokers/alpaca.py` pattern
   ([`tests/test_live_backend.py`](../../tests/test_live_backend.py)).

2. **Typed error vocabulary.** A single `_request()` retry loop
   classifies every response:
   - HTTP **401/403** → `BrokerAuthError` (permanent; live loop engages
     the kill-switch before the next order).
   - HTTP **408/425/429/500/502/503/504** and httpx transport errors →
     retried with exponential backoff, then `BrokerConnectionError`
     (caller wraps `submit` in retry-with-backoff).
   - Other **4xx** → `BrokerRejectError` with the broker's
     `{code, message}` body surfaced as `broker_code`
     (insufficient buying power, unknown symbol, …).

3. **Paper-by-default base URL.** `paper=True` (the default) resolves
   to `https://paper-api.alpaca.markets`. Reaching the real-money
   endpoint (`https://api.alpaca.markets`) requires an explicit
   `paper=False` **and** an explicit `base_url` override, so a
   misconfigured deploy cannot accidentally route a real order.

### Duplicate-order safety (idempotency key)

`submit_order` always sends a `client_order_id` — a caller-supplied
value or a freshly minted `uuid4` generated *before* the HTTP call. The
broker de-duplicates on this key, so a resubmitted/retried order is
rejected as a duplicate rather than double-placed. Critically, the retry
loop does **not** retry a `POST /v2/orders` on a transport error: a
transport failure on that non-idempotent path raises
`BrokerConnectionError` immediately with a reason that names the
duplicate-avoidance intent. This is the fix shipped in gh#1121.

### Wiring status

`LiveExecutionBackend` is implemented, exported from
[`engine/execution/__init__.py`](../../engine/execution/__init__.py),
and unit-tested. It is **not yet**:

- registered in [`engine/core/execution/factory.py`](../../engine/core/execution/factory.py)
  — `create_backend("live")` still returns the scaffold
  `engine/core/execution/live.py:LiveBackend`;
- reachable from any HTTP route — there is no live/paper `run`
  endpoint in [`engine/api/router.py`](../../engine/api/router.py).

So today the class is a tested internal preview; the OMS cannot reach it
through the public surface. See
[`known-limitations.md`](../known-limitations.md) "Three Execution
Modes".

### Consequences

- **Positive** — the live write path is real, typed, and testable; the
  `BrokerAdapter`/`LiveLoop`/kill-switch machinery in
  [`engine/core/live/`](../../engine/core/live/) has a concrete backend
  to talk to.
- **Positive** — duplicate-order safety is structural (idempotency key
  + no-retry-on-non-idempotent-POST), not a convention operators must
  remember.
- **Positive** — the scaffold and the factory's `live` name are
  untouched, so nothing that imports `engine.core.execution` breaks.
- **Negative** — there are now *two* live backend classes
  (`engine.core.execution.live.LiveBackend` scaffold vs.
  `engine.execution.live_backend.LiveExecutionBackend` concrete). The
  factory must be repointed at the concrete one before the scaffold is
  removed; until then the naming overlap is a footgun. Documented in
  `known-limitations.md`.
- **Negative** — Alpaca-compatibility is by REST shape, not a vendored
  SDK, so breaking changes to the Alpaca order body surface require a
  code change here rather than a dependency bump.

## Pros and Cons of the Options

### Option 1 — Promote the scaffold

- **Pros:** No new file; one place for "live backend".
- **Cons:** The scaffold has no broker wiring and flips
  `_is_scaffold` to gate credential validation; bolting Alpaca REST
  onto it would couple a transport choice into a class that is meant
  to be broker-agnostic. Two distinct concerns (scaffold-vs-concrete,
  ABC-vs-broker-direct) would share one class.

### Option 2 — Vendor SDK (`alpaca-py`)

- **Pros:** Less order-body code to maintain; SDK handles auth,
  pagination, retry.
- **Cons:** Adds a heavyweight transitive dependency for a single
  adapter; hides the exact wire format behind an abstraction, making
  the typed-error mapping harder; the SDK's retry policy can resubmit
  non-idempotent `POST /v2/orders` and reintroduce the duplicate-order
  risk this ADR's idempotency rule exists to prevent; breaks
  testability parity with the `MockTransport` pattern the rest of the
  codebase uses.

### Option 3 — Alpaca-compatible REST + injectable client (chosen)

- **Pros:** Testability parity with the data/broker layers; explicit,
  reviewable retry policy that knows which paths are non-idempotent;
  no new runtime dependency; paper-by-default makes a real-money
  misroute impossible without two explicit overrides.
- **Cons:** We own the order-body serialisation; two live-backend
  classes until the factory is repointed (documented limitation above).

## Links

- Feature PRs: gh#1117 (`feat(execution): add LiveExecutionBackend with
  Alpaca API support`), gh#1121 (`fix(execution): add client_order_id
  to prevent duplicate orders`), gh#1119 (tests).
- Source: [`engine/execution/live_backend.py`](../../engine/execution/live_backend.py),
  [`engine/execution/__init__.py`](../../engine/execution/__init__.py),
  [`engine/core/brokers/base.py`](../../engine/core/brokers/base.py)
  (error hierarchy).
- Tests: [`tests/test_live_backend.py`](../../tests/test_live_backend.py).
- Companion driver: [`engine/core/live/loop.py`](../../engine/core/live/loop.py)
  (`LiveLoop` — submit + broker-event consumption; kills the engine on
  `BrokerAuthError`).
- Status: [`docs/known-limitations.md`](../known-limitations.md)
  "Three Execution Modes (Roadmap: partial)".
