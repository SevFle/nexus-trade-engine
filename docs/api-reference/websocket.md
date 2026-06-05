# WebSocket stream

The engine exposes a single WebSocket endpoint for streaming
real-time events to authenticated clients. Source:
[`engine/api/routes/websocket.py`](../../engine/api/routes/websocket.py),
[`engine/api/websocket/`](../../engine/api/websocket/).

WebSocket support is currently **partial** — the bridge and
manager are wired, the auth flow works, but only a handful of
events are streamed (backtest progress, order fills). Live market
data streaming is on the roadmap.

## Endpoint

### `WS /api/v1/ws`

**Auth:** JWT, passed as a query parameter:

```
wss://host/api/v1/ws?token=<jwt>
```

The handler rejects the upgrade with `4401 Unauthorized` if the
token is missing or invalid (we close the socket with code `4401`
because the standard `401` is not a valid WebSocket close code).

## Message protocol

Client → server messages are JSON of the shape:

```json
{ "action": "subscribe", "topic": "backtest.<id>" }
{ "action": "unsubscribe", "topic": "backtest.<id>" }
{ "action": "ping" }
```

Server → client messages:

```json
{ "type": "event", "topic": "backtest.<id>",
  "payload": { "phase": "running", "progress": 0.42 } }
{ "type": "ack", "action": "subscribe", "topic": "..." }
{ "type": "error", "code": "unknown_topic", "message": "..." }
{ "type": "pong" }
```

## Topic vocabulary

| Topic pattern           | Payload                                  |
|-------------------------|------------------------------------------|
| `backtest.<backtest_id>` | `{ phase, progress, error? }`           |
| `portfolio.<portfolio_id>` | portfolio-level events (positions, orders) |
| `system`                | Engine-wide notices (kill switch, etc.) |

Topics are enforced server-side: the connection's user must own
the resource the topic namespacing refers to, otherwise `ack` is
`error: forbidden`.

## Connection lifecycle

- The server sends a `pong` for every `ping`. We do not enforce
  a client-side heartbeat yet — set one in your reverse proxy
  (nginx `proxy_read_timeout`, Envoy `idle_timeout`) before
  relying on the socket for alerting.
- Connection closure drops all subscriptions. The client must
  resubscribe after reconnect.
- The bridge stores no history. If a client connects after an
  event has fired, it does not receive that event.

## Operational caveats

- Only one process can serve a given client reliably today. If
  you scale the engine horizontally behind a load balancer
  without sticky sessions, the client may connect to a process
  that does not own the worker emitting the event. The fix is
  either sticky sessions or a pub/sub backplane (Redis Streams
  is the planned choice).
- The WebSocket handler does **not** go through
  `BodySizeLimitMiddleware` — message size is bounded by
  uvicorn's `--limit-concurrency` / per-frame defaults only.
