# Brokers, OMS, and the live-trading loop

The codebase contains a complete **order-management + broker-adapter**
subsystem under [`engine/core/brokers/`](../../engine/core/brokers/),
[`engine/core/oms/`](../../engine/core/oms/), and
[`engine/core/live/`](../../engine/core/live/). It is the foundation
for live and paper execution. **It is not yet reachable from a public
HTTP route** — see [`known-limitations.md`](../known-limitations.md) —
so this page documents an internal surface that future run-routes and
the worker will bind.

Read this page to understand the *shape* of execution before it ships,
and where to slot new broker integrations.

## Where it sits

```mermaid
flowchart LR
    SUB["submit(order)"] --> LOOP["LiveLoop<br/>engine/core/live/loop.py"]
    LOOP -->|"pre-flight"| RISK["RiskGate<br/>oms/risk.py"]
    RISK -->|Approve| ADP["BrokerAdapter<br/>brokers/base.py"]
    RISK -->|Reject| REJ["RejectEvent → order"]
    ADP -->|submit| BR["Broker<br/>(Alpaca / Paper)"]
    BR -->|events\\(\\)| EV["OrderEvent stream<br/>(Ack/Fill/Cancel…)"]
    EV --> LOOP
    LOOP -->|"apply_event"| ORD["Order state machine<br/>oms/order.py · states.py"]
    ORD -->|every transition| P["persister\\(\\) callback"]
    KS["KillSwitch<br/>(global singleton)"] -.->|engaged → Reject| RISK
```

The loop is the smallest unit of "submit an order, then react to broker
events". Everything to the left of `submit` (signal generation, the
strategy orchestrator) and to the right of `persister` (tax-lot
updates, WebSocket fan-out) is deliberately **not** in this subsystem —
the loop is a pure driver over three collaborators.

## The broker layer (`engine/core/brokers/`)

Two Protocols, deliberately split. Concrete adapters implement one or
both; the loop only depends on the abstractions.

### `BrokerClient` — the wire contract ([`models.py`](../../engine/core/brokers/models.py))

The low-level per-broker client. It is the broker-neutral shape of
"talk to one broker's REST/streaming API":

| Method | Returns | Notes |
|---|---|---|
| `connect()` / `close()` | `None` | Lifecycle. `connect()` must validate credentials cheaply (Alpaca hits `GET /v2/account`). |
| `submit_order(BrokerOrderRequest)` | `BrokerOrderStatus` | The broker's immediate status snapshot. |
| `get_order(broker_order_id)` | `BrokerOrderStatus` | Poll a single order's fill state. |
| `cancel_order(broker_order_id)` | `None` | Broker may still fill a cancel race — that shows up as a later fill event. |
| `get_clock()` | `BrokerClock` | Market open/closed + next transitions. Lets the loop avoid submitting into a closed market. |
| `get_position(symbol)` | `BrokerPosition` | Qty + cost basis + unrealised PnL. |

DTOs (`BrokerOrderRequest`, `BrokerOrderStatus`, `BrokerClock`,
`BrokerPosition`) are frozen dataclasses with `from_response()`
constructors that tolerate the messy reality of vendor payloads (missing
keys, bad numeric strings, `Z`-suffixed timestamps). Money/qty fields
are `Decimal` end to end — never `float`.

### `BrokerAdapter` — the OMS contract ([`base.py`](../../engine/core/brokers/base.py))

The higher-level Protocol the `LiveLoop` depends on. Three methods:

- `submit(order) -> SubmittedOrder` — send the OMS `Order`, get the
  broker's `broker_order_id` back so subsequent events correlate.
- `cancel(*, order_id, broker_order_id)` — request cancellation.
- `events() -> AsyncIterator[OrderEvent]` — the broker's event stream,
  **already translated** into OMS event types (`AckEvent`,
  `PartialFillEvent`, `FillEvent`, `CancelEvent`, …). This is the key
  seam: adapters own the translation from vendor-native shapes to the
  OMS vocabulary, so the loop never sees broker-specific types.

`SubmittedOrder` carries both the OMS-side `order_id` and the broker's
`broker_order_id` — that pair is the correlation key for the whole
event-driven flow.

### Error vocabulary ([`base.py`](../../engine/core/brokers/base.py))

Three exception classes, because the loop reacts to each differently:

| Exception | Meaning | Loop reaction |
|---|---|---|
| `BrokerAuthError` | Credentials rejected (HTTP 401/403). | **Engage the kill-switch** and re-raise. Systemic — do not retry. |
| `BrokerConnectionError` | Transient network / 5xx / 429. | Log and re-raise. Caller wraps `submit` in retry-with-backoff. |
| `BrokerRejectError` | Broker accepted the request but rejected the *order* (margin, restricted list, bad price). Carries `broker_code`. | Apply `RejectEvent` to the order. **Does not** engage the kill-switch — it's per-order. |

This split is load-bearing: a permission failure is a "stop the world"
condition, a margin rejection on one order is not. Do not collapse them.

### Concrete adapters

| Adapter | File | Status |
|---|---|---|
| `AlpacaTradingClient` | [`brokers/alpaca/__init__.py`](../../engine/core/brokers/alpaca/__init__.py) | Implemented + unit-tested (gh#136, #1010). Talks to Alpaca's trading REST API directly over `httpx` — **no `alpaca-py` dependency**. Paper (`paper-api.alpaca.markets`) and live URLs are selected by the `paper=` flag. The HTTP client is injectable (`client=`) so tests swap in an `httpx.MockTransport`. |
| `PaperBroker` | [`brokers/paper.py`](../../engine/core/brokers/paper.py) | In-process simulated broker. Market orders ack+fill immediately via an operator-supplied `price_for(symbol)` callable; limit/stop orders rest and are filled by `simulate_fill()` (test helper). No order book, no slippage — that belongs in the backtest runner. |

`AlpacaTradingClient._request` centralises the retry/error policy:
transient statuses (`408,425,429,500,502,503,504`) and `httpx`
transport errors retry up to `max_retries` with exponential backoff
then raise `BrokerConnectionError`; auth statuses (`401,403`) raise
`BrokerAuthError` immediately; everything else ≥400 is a
`BrokerRejectError` carrying Alpaca's numeric `code` as `broker_code`.

### Registry ([`brokers/registry.py`](../../engine/core/brokers/registry.py))

A thread-safe, process-global name → adapter map. Register once at
startup under a stable lower-case slug; the loop / routes look one up
by name at runtime.

```python
from engine.core.brokers.alpaca import AlpacaTradingClient
from engine.core.brokers.registry import register_broker

register_broker(AlpacaTradingClient(api_key, api_secret, paper=True))
```

`register_broker` validates that the argument implements
`BrokerAdapter` and that the name is lower-case; re-registering a name
overwrites (intentional — lets operators swap paper↔live at startup).

## The OMS (`engine/core/oms/`)

The order state machine. It is **broker-agnostic and pure**: it knows
nothing about HTTP or any vendor.

| File | Responsibility |
|---|---|
| [`states.py`](../../engine/core/oms/states.py) | `OrderStatus` enum + `VALID_TRANSITIONS`. The single source of truth for which state may follow which. |
| [`order.py`](../../engine/core/oms/order.py) | The `Order` aggregate + `apply_event(event)` that walks the state machine. |
| [`events.py`](../../engine/core/oms/events.py) | Frozen-dataclass event vocabulary (`SubmitEvent`, `AckEvent`, `PartialFillEvent`, `FillEvent`, `CancelEvent`, `RejectEvent`). One event = one external fact. |
| [`risk.py`](../../engine/core/oms/risk.py) | `RiskGate` — pre-flight checks that run *before* submit. |
| [`persistence.py`](../../engine/core/oms/persistence.py) | Persistence boundary (the `persister` callback contract). |

### Order lifecycle

```
NEW → SUBMITTED → ACKNOWLEDGED → PARTIALLY_FILLED* → FILLED
                                       ↘ CANCEL_REQUESTED → CANCELLED
Terminal: FILLED · CANCELLED · REJECTED · EXPIRED
```

`VALID_TRANSITIONS` is exported so monitoring code can validate a
broker-emitted event without re-reading the implementation. Reverse
transitions are intentionally excluded — once `CANCELLED`, an order
never becomes `ACKNOWLEDGED` again; mint a new order to retry. The
notable subtlety is `CANCEL_REQUESTED → FILLED`: a broker can fill an
order *before* it processes the cancel, and the state machine must
accept that (it does).

### Pre-flight risk (`RiskGate`)

An ordered list of `RiskCheck`s run before submit. Any check returning
`Reject` short-circuits; the OMS transitions the order via
`RejectEvent` and the broker is never called. Keeping risk policy
**separate from the state machine** keeps the machine pure and lets
operators compose their own check chain.

Built-in checks:

- `KillSwitchCheck` — refuses when the global switch is engaged.
- `MaxOrderQuantity` — per-order qty ceiling.
- `MaxOrderNotional` — `qty × reference_price` ceiling (caller supplies
  the reference price, typically last trade / mid-quote).

Explicit non-goals (documented in the module): per-symbol position
caps, per-strategy max-drawdown, buying-power/margin pre-flight,
restricted-list enforcement, crypto 24h caps. Those need portfolio /
account / compliance state that isn't wired yet.

## The live loop ([`engine/core/live/loop.py`](../../engine/core/live/loop.py))

`LiveLoop` is the submit-and-consume orchestrator:

1. `submit(order, reference_price=None)` runs the `RiskGate`.
2. On `Reject`, the order transitions to `REJECTED` via `RejectEvent`;
   the broker is never called.
3. On `Approve`, it calls `BrokerAdapter.submit`, records the
   `broker_order_id`, and holds the `Order` in an in-memory registry
   keyed by **both** the OMS id and the broker id.
4. The caller consumes `broker.events()` and hands each event to
   `apply_broker_event(event)`, which looks up the originating order,
   applies it through the state machine, and calls the operator-supplied
   `persister` callback with the post-event `Order`.

Error policy (matches the vocabulary above): `BrokerAuthError` engages
the kill-switch and re-raises; `BrokerRejectError` applies a
`RejectEvent` (no kill-switch); `BrokerConnectionError` is logged and
re-raised so the caller's retry wrapper handles it.

Documented follow-ups **not yet implemented**: startup reconciliation
(read open orders from the broker and walk events forward), recovery
from a half-submitted order (broker received but engine crashed before
persisting `broker_order_id`), multi-broker routing, and fill-handler
hooks for downstream tax-lot updates.

## Kill-switch ([`engine/core/live/kill_switch.py`](../../engine/core/live/kill_switch.py))

The safety floor. Any code path that submits an order **must** check it
first (enforced by `KillSwitchCheck` in the risk gate).

- One process-singleton (`get_kill_switch()`) so domain code and routes
  share a view without threading the instance through DI.
- `engage(reason, actor=…)` is **idempotent** — "smash the red button"
  must not race with itself.
- `disengage(confirmation=…)` requires an explicit token
  (`"I_UNDERSTAND_THE_RISK"`) so a stray script can't restart trading.
- Observers are notified on every transition; observer failures are
  logged but never block the transition — the switch must always work.

Non-goals (explicit): the switch does **not** survive a restart (the
loop is expected to re-read state on boot), there are no auto-engage
triggers yet (max-loss / drawdown / broker-disconnect policies belong
in orchestration and call `engage` when they trip), and the switch is
**global** today, not per-strategy/per-symbol.

## Honest status

This subsystem is real, tested code — but it has no public entry point.
Concretely:

- No HTTP route calls `LiveLoop.submit`. The execution routers under
  `engine/core/execution/` (`backtest.py`, `paper.py`, `live.py`) are
  the *intended* binding, but the live/paper backends are not wired to
  a run route (see [known-limitations.md](../known-limitations.md)).
- The kill-switch is in-memory only; a restart silently disengages it.
- No startup reconciliation against the broker exists.
- The `PaperBroker` is a broker stand-in, **not** a market simulator.

Treat all of this as the internal preview of live trading. The
backtest engine is the production surface today.
