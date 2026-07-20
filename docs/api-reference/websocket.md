# WebSocket API

The engine exposes **two** WebSocket endpoints, both under `/api/v1`.
They share the same channel taxonomy, permission model, and outbound
message schemas, but differ in how they authenticate and what they
deliver:

- [`WS /api/v1/ws`](#ws-channel-endpoint) — the generic
  channel pub/sub endpoint (SEV-275). Auth happens **after** the
  handshake, inside the message loop.
- [`WS /api/v1/ws/events`](#ws-events-endpoint) —
  streams [`EventBus`](../../engine/events/bus.py) events to subscribed
  clients. Auth happens **before** `ws.accept()`, at the HTTP layer.

Conventions that apply to both endpoints are documented once in
[`api-reference.md`](../api-reference.md) (auth model, error semantics,
middleware). This page covers only what is specific to the WS surface.

> **Legacy code, not mounted.** [`engine/api/routes/websocket.py`](../../engine/api/routes/websocket.py)
> is an older (gh#7) implementation that authenticates via the first
  JSON message and uses the `VALID_TOPICS` set (`portfolio`, `backtest`,
  `order`, `alert`) from [`engine/api/websocket/manager.py`](../../engine/api/websocket/manager.py).
  **It is not included in [`router.py`](../../engine/api/router.py)** —
  only its helper functions are exercised by
  [`tests/test_low_coverage_routes_sev264.py`](../../tests/test_low_coverage_routes_sev264.py).
  The `UserTopicManager` it depends on is still imported elsewhere, so
  the file is kept for now, but no client can reach it. The two sections
  below are the actual production surface.

---

<a id="ws-channel-endpoint"></a>
## `WS /api/v1/ws` — the channel endpoint

Source: [`engine/api/ws/router.py`](../../engine/api/ws/router.py).

### Connection lifecycle

1. Client opens `WS /api/v1/ws`. Server accepts unconditionally.
2. Server authenticates via [`authenticate_websocket`](../../engine/api/ws/auth.py).
   On failure, sends an `ErrorMessage` and closes with one of the
   `4401`/`4402`/`4404`/`4451` codes (see [close codes](#close-codes)).
3. On success, the server registers the connection with the
   [`ConnectionManager`](../../engine/api/ws/connection_manager.py)
   and replies `{"type":"ack","status":"ok","message":"connected"}`.
4. The client drives the message loop with `subscribe` / `unsubscribe`
   / `ping` messages; the server multiplexes `event` messages back.
5. Either side may close. The server `unregister`s on disconnect.

### Auth (post-accept)

[`authenticate_websocket`](../../engine/api/ws/auth.py) pulls a token
from, in priority order:

1. `Authorization: Bearer <token>` header (preferred — mirrors REST).
2. `Sec-WebSocket-Protocol` subprotocol: `bearer.<token>` or a bare
   token. Use this from browsers that can't set request headers on a
   WS handshake.
3. `token` query parameter — lowest-priority handshake fallback.
   **Query strings are logged by proxies, load balancers and browser
   history**, so prefer the header or subprotocol path whenever
   possible.
4. The first JSON message `{"type":"auth","token":"..."}` within
   `auth_timeout` seconds — retained for back-compat.

The token can be a JWT or an engine API key (`nxs_...`); both decode
through the same `decode_token` path. Per-IP auth attempts are bounded
by an `AuthRateLimiter` (token bucket, default 10 attempts / 60s).

### Inbound messages (client → server)

Defined in [`engine/api/ws/protocol.py`](../../engine/api/ws/protocol.py):

| `type` | Body | Effect |
|---|---|---|
| `auth` | `{token, ref?}` | Mid-session token refresh. `validate_refresh_token` checks the refresh token; on success the session's `user_id`/`scopes` are rotated and an `ack` is returned. On failure the socket is closed with `4403`. |
| `subscribe` | `{channel, params:{}, ref?}` | Join the room resolved from `channel`+`params`. See [channels](#channels-and-rooms). |
| `unsubscribe` | `{channel, params:{}, ref?}` | Leave the matching room. |
| `ping` | `{ref?}` | Server replies `pong` with the same `ref`. Used for keepalive. |

Anything else → `ErrorMessage` with `code:"PARSE_ERROR"` or
`"INVALID_MESSAGE"`. Unknown message `type` → `PARSE_ERROR` with a
description.

### Channels and rooms

Three channels are valid (see `VALID_CHANNELS`):

| Channel | Required scope | Owner field | Room keys |
|---|---|---|---|
| `portfolio` | `read:portfolio` | `account_id` | `portfolio:account:<id>`, `portfolio:strategy:<id>` |
| `orders` | `read:orders` | `account_id` | `orders:symbol:<sym>`, `orders:status:<status>` |
| `strategies` | `read:strategies` | `strategy_id` | `strategies:strategy:<id>` |

Permission logic lives in
[`engine/api/ws/permissions.py`](../../engine/api/ws/permissions.py):

- Having the channel's `:all` scope (e.g. `read:portfolio:all`) grants
  access to every room in that channel.
- Otherwise the base scope (e.g. `read:portfolio`) grants access only
  when the `owner_field` in `params` matches the authenticated
  `user_id` — i.e. users see their own data, operators with `:all`
  see everyone's.
- Missing scope → `AckMessage` with `status:"error"`, `error_code:"403"`.
- Unknown channel → `error_code:"404"`.
- Missing required `params` → `error_code:"400"`.
- More than 50 non-`user:` rooms on one connection → `error_code:"429"`.

The room name is `<prefix>:<value>` (e.g. `portfolio:account:42`); the
`ConnectionManager` keeps `{connection_id → rooms}` and
`{room → connection_ids}` maps so broadcast is O(subscribers).

### Outbound messages (server → client)

| `type` | Shape | When |
|---|---|---|
| `ack` | `{ref?, status:"ok"\|"error", error_code?, message?}` | Response to `subscribe`/`unsubscribe`/`auth`. |
| `event` | `{channel, room, payload:{}, seq:int, ts:ISO8601}` | A subscribed event was delivered. `seq` is per-connection monotonic. |
| `pong` | `{ref?}` | Response to `ping`. |
| `error` | `{code, message, ref?}` | Recoverable protocol error. |
| `close` | `{code, reason}` | Server-initiated close (rare; usually the server just closes the socket). |

---

<a id="ws-events-endpoint"></a>
## `WS /api/v1/ws/events` — the events endpoint

Source: [`engine/api/ws/events.py`](../../engine/api/ws/events.py).

This is the streaming surface that fans out
[`EventBus`](../../engine/events/bus.py) events to subscribed clients.
The crucial difference from `/ws` is **pre-accept auth**.

### Connection lifecycle

1. Client opens `WS /api/v1/ws/events?token=<jwt>` (or
   `?session_token=<jwt>`). **The token must be a JWT** — API keys
   are not accepted on this endpoint (there's no DB session at
   handshake time to look one up).
2. `_validate_session_token` runs `decode_token` **before** `ws.accept()`.
   A bad/expired/missing token → the handshake is rejected with close
   code `4401` and the socket is never upgraded.
3. On success the server accepts, registers the connection, and sends
   `{"type":"ack","status":"ok","message":"connected"}`.
4. Same message loop as `/ws` (`subscribe`/`unsubscribe`/`ping`),
   minus `auth` (refresh happens by reconnecting with a new token).

### Why the auth difference

A generic `/ws` has to accept the socket before it can read the first
message, so auth happens in-band. The events endpoint exists
specifically to stream sensitive account data, so it refuses the
handshake entirely on a bad token — the server never holds an
unauthenticated socket open. The trade-off is no in-band refresh: when
the JWT expires, the server closes with `4403` and the client
reconnects with a fresh token (obtained via `POST /api/v1/auth/refresh`).

### Channels, rooms, permissions

Identical to [`/ws`](#channels-and-rooms) — same `VALID_CHANNELS`,
same permission matrix, same room-naming convention. The two endpoints
share the `ChannelResolver` and `ConnectionManager` code; only the
auth path differs.

<a id="cross-replica-event-delivery"></a>
### Cross-replica event delivery

Each engine replica has its own process-local `ConnectionManager`, so
a client connected to replica A doesn't see events emitted on replica B
by default. The [`EventBusBridge`](../../engine/api/ws/event_bridge.py)
fixes this: it subscribes to the local `EventBus` and **publishes every
event onto a Valkey pub/sub channel** that all replicas listen to.
Each replica then fans the event out to its own locally-connected
subscribers.

This means a strategy running on replica B can emit a `portfolio.update`
event and have it reach a client subscribed on replica A. The bridge is
wired during app startup in [`engine/app.py`](../../engine/app.py); see
[ADR 0009](../adr/0009-cross-replica-eventbus-bridge.md) for the design.

Wiring detail: [`init_ws_events`](../../engine/api/ws/events.py) captures
the running event loop *first*, before anything else, so any component
that needs to schedule onto it has it available the moment init returns.
It's also re-init safe — calling it again disconnects every existing
client and stops the previous bridge before installing the new one,
so a config reload can't leak connections or double-subscribe to the
bus.

---

## Close codes

Defined in [`engine/api/ws/protocol.py`](../../engine/api/ws/protocol.py).
Custom codes live in the `4xxx` range to avoid colliding with the
RFC 6455 standard codes:

| Code | Meaning | When |
|---|---|---|
| `1000` | Normal close | Either side ended the session cleanly. |
| `1008` | Policy violation | Generic protocol violation. |
| `1011` | Server error / not ready | `init_ws` / `init_ws_events` not called, or unhandled exception in the loop. |
| `4401` | Auth invalid / timeout | Bad token, or no auth message within `auth_timeout`. |
| `4402` | Auth timeout | Specifically: no auth message received in time (older gh#7 path; the SEV-275 path collapses this into `4401`). |
| `4403` | Token expired | Refresh token failed validation; client must reconnect with a new token. |
| `4404` | Auth forbidden | Mirrors HTTP 403 — token decoded but lacks the required scope. |
| `4451` | Legal re-acceptance required | Mirrors HTTP 451 — pending legal re-acceptance blocks the session. |

The codes mirror their HTTP counterparts (`401`/`403`/`451`) on the
`4` prefix so the semantics are obvious from the number.

---

## Operational notes

- **Per-process registry.** The `ConnectionManager` is in-process state.
  A client connected to replica A is invisible to replica B until an
  event traverses the cross-replica bridge (see above). Connection
  counts are *not* shared, so any future "connected clients" metric
  must aggregate across replicas.
- **Heartbeats.** The server replies to every `ping` with a `pong`
  (same `ref`). There is no server-initiated heartbeat today — clients
  are expected to ping on their own keepalive schedule. A connection
  that goes quiet is eventually reaped by the manager's idle sweep.
- **Rate limits.** The REST `RateLimitMiddleware` does not apply to
  WebSocket handshakes (it's HTTP-layer). Per-IP auth-attempt limiting
  is handled by `AuthRateLimiter` inside `authenticate_websocket`.
  Message-rate limiting per connection is **not** implemented today.
- **Metrics.** `sev_ws_messages_received_total{type=...}` counts
  inbound messages by type; `sev_ws_auth_failures_total{reason=...}`
  counts handshake/auth failures. Both are emitted via the shared
  [`ws_metrics`](../../engine/api/ws/metrics.py) handle and surface in
  `/metrics` when a `RecordingBackend` is wired.

---

## Minimal client example

```javascript
// 1. Open the events endpoint with a JWT in the query string.
const ws = new WebSocket(
  "/api/v1/ws/events?token=" + encodeURIComponent(jwt)
);

ws.onopen = () => {
  // 2. Subscribe to portfolio updates for account 42.
  ws.send(JSON.stringify({
    type: "subscribe",
    channel: "portfolio",
    params: { account_id: "42" },
    ref: "sub-1",
  }));
};

ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.type === "event" && msg.channel === "portfolio") {
    console.log("portfolio update", msg.room, msg.payload, msg.seq);
  } else if (msg.type === "ack") {
    console.log("ack for", msg.ref, msg.status);
  }
};

// 3. Keep the connection warm.
setInterval(() => ws.send(JSON.stringify({ type: "ping" })), 30_000);
```

The same client works against `/api/v1/ws` if you swap the URL and
authenticate via the first message instead of the query string —
the rest of the protocol is identical.
