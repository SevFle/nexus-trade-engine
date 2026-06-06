# WebSocket API

Real-time push channel for events on the caller's behalf.
Implementation: [`engine/api/routes/websocket.py`](../../engine/api/routes/websocket.py),
[`engine/api/websocket/manager.py`](../../engine/api/websocket/manager.py).

The protocol is *auth-then-subscribe*: the connection is accepted
unauthenticated, but the first inbound message must be an `auth` frame
within `AUTH_TIMEOUT_SECONDS` (10 s). JWT-in-URL is intentionally
**not** supported because URLs end up in proxy logs.

## Endpoint

| Method | Path | Purpose |
|---|---|---|
| `WS` | `/api/v1/ws` | Bidirectional event stream |

## Handshake

```text
Client                                Server
  │ ────── WS upgrade ─────────────▶    │  accept
  │                                     │
  │ ────── {"type":"auth",              │
  │         "token":"<jwt or nxs_*>"} ─▶│  validate
  │                                     │
  │ ◀────── {"type":"auth.ok",          │
  │          "user_id":"..."} ──────────│
  │                                     │
  │ ────── {"type":"subscribe",         │
  │         "topics":["portfolio",...]}▶│  attach to manager
  │                                     │
  │ ◀────── {"type":"subscribed",       │
  │          "topics":[...]} ───────────│
```

If the first frame is not `auth` (or it arrives after 10 s) the socket
closes with code `4400` / `4401`. Server-side close codes are in the
`4400-4499` block to avoid colliding with the standard `1000-1011`
range that browsers handle specially.

## Auth tokens

Either credential works as the `token` field:

- A JWT access token (the same one used for `Authorization: Bearer`).
- An engine API key (`nxs_<env>_<random>`).

The dispatcher checks the `nxs_` prefix and routes to the API-key
verifier or the JWT verifier. Refresh tokens are **never** accepted
here — they are single-use and rotate on every `/auth/refresh`.

## Message types (client → server)

| `type`        | Payload                                  | Server response |
|---------------|------------------------------------------|-----------------|
| `auth`        | `{"token": "..."}`                       | `auth.ok` or close `4401` |
| `subscribe`   | `{"topics": ["portfolio", "backtest"]}`  | `subscribed` |
| `unsubscribe` | `{"topics": [...]}`                      | `unsubscribed` |
| `ping`        | —                                        | `pong` |
| anything else | —                                        | `error` (`code: "unknown_message_type"`) |

## Topics

The valid topic set is `VALID_TOPICS` in
[`engine/api/websocket/manager.py`](../../engine/api/websocket/manager.py).
Unknown topics in a `subscribe` payload are silently dropped (the
returned `topics` list shows the actually-subscribed set). Today the
canonical topics are:

- `portfolio` — position changes, fills, PnL snapshots
- `backtest`  — status transitions (`running` → `completed` / `failed`)
- `webhook`   — delivery status changes
- `system`    — process-level events (kill switch, deployment reload)

To add a new topic: register it in `VALID_TOPICS`, then publish via
`manager.publish(topic, payload)` from the relevant domain code.

## Server → client frames

```json
{ "type": "subscribed",   "topics": ["portfolio"] }
{ "type": "unsubscribed", "topics": [] }
{ "type": "pong" }
{ "type": "auth.ok",      "user_id": "..." }
{ "type": "error",        "code": "unknown_message_type", "detail": "..." }
{ "type": "event",        "topic": "portfolio", "payload": { ... } }
```

(The exact envelope of an `event` frame is defined in
`engine/api/websocket/manager.py:publish`.)

## Close codes

| Code  | Reason              |
|-------|---------------------|
| `4400` | `auth_required` (first frame not auth) / `auth_token_missing` |
| `4401` | `auth_invalid` / `auth_timeout` |
| `1000` | Normal close from either side |

## Implementation notes

- One `WebSocketManager` per process; connections live in-process
  (no fan-out across replicas today). For horizontal scaling, route
  through a sticky-session load balancer or wait for the planned
  Redis-PubSub bridge (see [`../known-limitations.md`](../known-limitations.md)).
- The handler is wrapped in `try/finally` so a crashed message-loop
  still detaches the socket from the manager.
- The server suppresses send errors when the socket is already closed
  (`contextlib.suppress(Exception)` around `send_json`). This keeps a
  racing disconnect from masking the original error in logs.

## Example client (Python)

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect("ws://localhost:8000/api/v1/ws") as ws:
        await ws.send(json.dumps({"type": "auth", "token": "<jwt or nxs_*>"}))
        print(await ws.recv())                              # auth.ok
        await ws.send(json.dumps({"type": "subscribe",
                                  "topics": ["portfolio"]}))
        print(await ws.recv())                              # subscribed
        async for msg in ws:
            print(msg)                                      # event frames

asyncio.run(main())
```
