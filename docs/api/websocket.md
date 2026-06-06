# WebSocket API

Endpoint: `WS /api/v1/ws`. Source:
[`engine/api/routes/websocket.py`](../../engine/api/routes/websocket.py),
[`engine/api/websocket/manager.py`](../../engine/api/websocket/manager.py).

Real-time event stream. The client authenticates over the socket
itself (no JWT in the URL — those leak into proxy logs), subscribes
to topics, and receives server-pushed events.

## Connection lifecycle

```
client                                                server
  │                                                     │
  │ ─── WS upgrade to /api/v1/ws ─────────────────────▶ │
  │ ◀─── 101 Switching Protocols ────────────────────── │
  │                                                     │
  │ ──── {"type":"auth","token":"<JWT or nxs_*>"} ───▶ │
  │ ◀─── {"type":"auth.ok","user_id":"..."} ────────── │
  │                                                     │
  │ ──── {"type":"subscribe","topics":["backtests"]} ▶ │
  │ ◀─── {"type":"subscribed","topics":["backtests"]} │
  │                                                     │
  │ ◀─── {"type":"event","topic":"backtests",          │
  │       "event_type":"backtest.completed",            │
  │       "payload":{...}}                              │
  │                                                     │
  │ ──── {"type":"ping"} ───────────────────────────▶ │
  │ ◀─── {"type":"pong"} ──────────────────────────── │
```

## Auth

Step 1 within `AUTH_TIMEOUT_SECONDS` (10 s) of opening the socket.
The client must send:

```json
{ "type": "auth", "token": "<JWT or nxs_<env>_<hex>>" }
```

Both credential shapes work:

- JWT — decoded and the `sub` claim resolved to a user.
- API key — looked up by `nxs_` prefix and bcrypt-verified.

The server replies with:

```json
{ "type": "auth.ok", "user_id": "uuid" }
```

If the credential is missing, malformed, expired, or unknown, the
server closes the socket with code `4401`. If no auth message arrives
within 10 s, the server closes with `4401 reason="auth_timeout"`. If
the message is not an auth message, the server closes with `4400
reason="auth_required"`.

## Subscribe / unsubscribe

After `auth.ok`, the client may send:

```json
{ "type": "subscribe", "topics": ["backtests", "portfolios"] }
```

The server echoes the resulting set:

```json
{ "type": "subscribed", "topics": ["backtests", "portfolios"] }
```

`unsubscribe` has the same shape and produces an `unsubscribed` ack.
Unknown topic names are silently dropped by `_coerce_topic_list` —
they do not error the connection.

## Valid topics

The valid set is `VALID_TOPICS` in
[`engine/api/websocket/manager.py`](../../engine/api/websocket/manager.py).
Today: `backtests`, `portfolios`, `webhooks`, `system`. Adding a
topic = extending the set + wiring an emitter in the relevant route
or event listener.

## Inbound events

Once subscribed, the client receives:

```json
{
  "type": "event",
  "topic": "backtests",
  "event_type": "backtest.completed",
  "payload": { "backtest_id": "uuid", "...": "..." }
}
```

Payloads are not in this document's scope — they match whatever the
producer emits. See `engine/events/bus.py` for the canonical event
catalogue.

## Ping / pong

Either side may send `{"type": "ping"}` to test liveness. The peer
replies with `{"type": "pong"}`. There is no application-level
keepalive policy today — rely on the WebSocket protocol's own pings
via the reverse proxy.

## Errors

If the client sends a message with an unknown `type`:

```json
{ "type": "error", "code": "unknown_message_type", "detail": "<the mtype>" }
```

The connection is not closed on unknown messages.

## Scaling

The WebSocket manager is in-process; events published to one engine
instance only reach clients connected to that instance. For a
multi-instance deploy, add a pub-sub backplane (Valkey
`PUBLISH`/`SUBSCRIBE` is the natural choice given the existing
infrastructure). Until then, sticky sessions at the load balancer
keep a user's events routed to the instance they are connected to —
which works because the engine is single-tenant-per-deployment today.
