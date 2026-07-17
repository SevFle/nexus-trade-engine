# WebSocket API

The real-time streaming surface — two endpoints (`/api/v1/ws` and
`/api/v1/ws/events`), the channel/room model, the role-derived scope
rules, and the cross-replica event bridge. This is the dedicated
reference; the REST surface is in [api-reference.md](api-reference.md).

Both endpoints are mounted by
[`engine/api/router.py`](../engine/api/router.py) and implemented in the
`engine/api/ws/` package:

| File | Role |
|---|---|
| [`ws/router.py`](../engine/api/ws/router.py) | Endpoint + message dispatch loop |
| [`ws/connection_manager.py`](../engine/api/ws/connection_manager.py) | Connection registry, room-based fan-out, heartbeat, backpressure |
| [`ws/channels.py`](../engine/api/ws/channels.py) | Resolves `subscribe` requests to rooms with permission checks |
| [`ws/permissions.py`](../engine/api/ws/permissions.py) | Channel access control + room-name resolution |
| [`ws/protocol.py`](../engine/api/ws/protocol.py) | Pydantic wire schemas + valid channel set |
| [`ws/event_bridge.py`](../engine/api/ws/event_bridge.py) | Subscribes to the `EventBus` and broadcasts events to rooms |
| [`ws/auth.py`](../engine/api/ws/auth.py) | In-band token validation + per-IP auth rate limiting |

> Note: `engine/api/routes/websocket.py` and
> `engine/api/websocket/manager.py` are a **legacy** implementation
> that is no longer mounted by [`router.py`](../engine/api/router.py).
> The active route comes from `ws/router.py`. Do not extend the legacy
> files.

## Handshake & message types

`WS /api/v1/ws`. Auth is JWT-only (the active `ws/auth.py` calls
`decode_token`; unlike the legacy endpoint it does **not** accept `nxs_*`
API keys). The token is delivered either as a `?token=` query param or
as the first JSON message within `NEXUS_WS_AUTH_TIMEOUT_SECONDS`
(default 5 s). The handshake:

```
client                                   server
  │── accept ────────────────────────────────▶│
  │── {"type":"auth","token":"<jwt>"} ────────▶│   (5 s window)
  │                                          │── {"type":"ack","status":"ok","message":"connected"}
  │── {"type":"subscribe","channel":"portfolio","params":{...}}─▶│
  │◀──── {"type":"ack","status":"ok","room":"portfolio:..."} ──│
  │◀──── {"type":"event","channel":...,"room":...,"payload":{...},"seq":N} ──│  (broadcasts)
  │── {"type":"ping","ref":"1"} ─────────────▶│── {"type":"pong","ref":"1"}
```

Inbound message types (see `protocol.py`): `auth`, `subscribe`,
`unsubscribe`, `ping`. Every message accepts an optional `ref` that
the server echoes back in the matching `ack`/`pong`.

Outbound message types: `ack`, `error`, `event`, `pong`, `close`.

## Channels (valid subscriptions)

| Channel | Sub-keyed by | Room shape |
|---|---|---|
| `portfolio` | account / strategy id | `portfolio:account:<id>`, `portfolio:strategy:<id>` |
| `orders` | symbol / status | `orders:symbol:<sym>`, `orders:status:<status>` |
| `strategies` | strategy id | `strategies:strategy:<id>` |

Each connection is also auto-joined to a private `user:<user_id>` room
on registration, so user-scoped events can be targeted directly.

## Auth & scopes

`authenticate_websocket` (`ws/auth.py`) accepts the JWT from either a
`?token=` query parameter or the first `auth` message. Prefer the
first-message form — query strings are recorded by reverse proxies and
log aggregators. Auth attempts are rate-limited per IP
(`NEXUS_WS_AUTH_RATE_LIMIT_PER_MINUTE`, default 10) via a token bucket.

Connection scopes are derived from the JWT `role` claim (see
[`ws/auth.py:_extract_scopes`](../engine/api/ws/auth.py#L80)):

| Role | Scopes granted |
|---|---|
| `admin`, `portfolio_manager` | base + `:all` for every channel |
| all others (`viewer` … `quant_dev`) | base `read:<channel>` only |

Permission checks (`ws/permissions.py`) run on every `subscribe`:

- `:all` scope → unrestricted access to the channel.
- base scope only → **owner-based** access: the channel's owner param
  (`account_id` / `strategy_id`) in `params` must equal the caller's
  `user_id`, else `403`.
- neither → `403`. Unknown channel → `error_code:"404"`. Subscription
  cap exceeded (`NEXUS_WS_MAX_SUBSCRIPTIONS_PER_CONNECTION`) → `429`.

Mid-session, a client can send `{"type":"auth","token":"<new JWT>"}` to
refresh an expiring token; the server re-derives scopes on the live
connection.

## Event delivery

[`ws/event_bridge.py`](../engine/api/ws/event_bridge.py)
(`EventBusBridge`) subscribes to the [`EventBus`](../engine/events/bus.py)
for portfolio / order / strategy event types and broadcasts each to the
matching room(s) as an `event` message with a per-room `seq`. Because
the `EventBus` itself publishes over Redis/Valkey pub/sub, events
published on **any** replica reach local WebSocket connections on
**every** replica. The `ConnectionManager` (the live socket objects) is
still per-process, but event distribution is cross-replica. See
[ADR 0009](adr/0009-cross-replica-eventbus-bridge.md) for the bridge
design.

## Second endpoint — `WS /api/v1/ws/events`

A second route, `WS /api/v1/ws/events` ([`ws/events.py`](../engine/api/ws/events.py)),
shares `/ws`'s `ConnectionManager`, `ChannelResolver`, `EventBusBridge`,
and wire protocol, but authenticates **before** `ws.accept()`: a
bad/missing query-param token rejects the handshake (close code `4401`,
reason `invalid session token`). `/ws/events` trades `/ws`'s in-band
first-message `auth` for fail-closed handshake auth.

- **Token**: `?token=<jwt>` (alias `?session_token=`), via the REST
  `decode_token` and shared `extract_scopes`. **JWT-only** (no `nxs_*`
  keys, same as `/ws`).
- **Server not ready**: if hit before `init_ws_events`, the socket is closed with code `1011` (`server not ready`).
- **Inbound**: `subscribe`, `unsubscribe`, `ping` (shared `parse_inbound`).
  **No mid-session refresh** — the token is bound to the handshake, so
  re-connect rather than re-auth.
- **Outbound**: same `ack` / `error` / `event` / `pong` / `close`; the
  channels, room shapes, and per-role scope rules above apply unchanged.
- **Wiring**: `init_ws_events(manager, resolver?, bridge?)` captures the
  running loop first; a re-init disconnects every client and stops the
  prior bridge, so a reload leaks no connections or double bus subscriptions.

Prefer `/ws/events` for fail-closed handshake auth; `/ws` when the token
can only arrive after the socket opens (in-band browser refresh).

> **Known limitation**: the `ConnectionManager` is process-local, so the
> *set of live sockets* is not shared across replicas — only the events
> they receive are. See
> [known-limitations.md](known-limitations.md#ws-process-local).
