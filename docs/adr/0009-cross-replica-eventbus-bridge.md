# ADR-0009: Cross-replica WebSocket event delivery via Redis pub/sub bridge

- **Status**: Accepted
- **Date**: 2026-06-09
- **Deciders**: Lead maintainer + realtime reviewer
- **Tags**: websocket, events, scaling, observability

## Context and Problem Statement

Nexus is designed to run as **multiple stateless replicas** behind a
load balancer (see [`deployment.md`](../deployment.md)). WebSocket
connections are inherently stateful: the live `WebSocket` objects live
in a per-process `ConnectionManager` dict. A client connected to
replica A will not receive an event that a domain handler emits on
replica B — unless something bridges the two.

Without a bridge, real-time updates (portfolio changes, order fills)
would only reach clients that happened to be connected to the same
replica where the event originated. That is unacceptable for a
trading platform: an order filled on the worker process must notify
the user's dashboard regardless of which `uvicorn` replica the
WebSocket is pinned to.

The requirement: a portfolio event published on **any** replica must
reach local WebSocket connections on **every** replica, without
sticky sessions or a shared connection registry.

## Decision Drivers

- **Correctness over latency.** It is better that an event arrives
  50 ms late on every replica than that it arrives instantly on one
  and never on the rest.
- **Valkey/Redis already in the stack.** TaskIQ uses it as a broker and
  the engine uses it as a cache. Reusing the same Valkey for pub/sub
  adds no new infrastructure.
- **No sticky sessions.** JWT auth means any replica can serve any
  request; forcing WS clients to a specific replica defeats
  horizontal scaling.
- **Graceful degradation.** If Redis is unavailable, the bus must
  keep working in-process-only (single-replica deployments must not
  hard-depend on Redis).

## Considered Options

1. **Sticky sessions at the LB** — pin each WS client to one replica;
   no bridge needed.
2. **Shared connection registry** (e.g. a Valkey hash of
   `user_id → replica_id`) plus direct replica-to-replica RPC.
3. **Redis/Valkey pub/sub fan-out** — the `EventBus` republishes every
   event onto a `nexus:<type>` channel; each replica's bridge
   subscribes and re-delivers to local rooms.
4. **NATS / Kafka** as a dedicated message bus.

## Decision Outcome

Chosen option: **Option 3 — Redis/Valkey pub/sub bridge**, via the
`EventBus` + `EventBusBridge` pair.

### How it works

The flow has two halves:

**Publish side** — [`engine/events/bus.py`](../../engine/events/bus.py):

`EventBus.publish()` does three things per event:
1. `await`s every registered in-process handler in turn (the webhook
   dispatcher is one such handler).
2. Republishes the event onto a Redis/Valkey pub/sub channel
   `nexus:<event_type>` (line 182). This is what makes it cross-replica.
3. Appends to an in-process ring buffer (`get_recent_events`).

If Redis is unavailable, `connect()` logs a warning and sets
`_redis = None`; `publish()` then skips step 2 and runs in-process-only.
A single-replica dev deploy keeps working without Redis.

**Consume side** — [`engine/api/ws/event_bridge.py`](../../engine/api/ws/event_bridge.py):

`EventBusBridge` subscribes to the relevant `EventType`s on the local
`EventBus` and maps each event type to a WebSocket channel
(`_EVENT_TO_CHANNEL`). For every event it resolves the target room
(via `resolve_room_name`), stamps a per-room sequence number, and
broadcasts an `EventMessage` to the room. Because the bridge subscribes
to the *local* bus — which already receives cross-replica events from
Redis pub/sub — a broadcast reaches every replica's local connections.

The bridge is concurrency-bounded by an `asyncio.Semaphore`
(`ws_event_bridge_concurrency`, default 32) so a burst of events does
not starve the event loop.

### Wiring

In the lifespan ([`app.py:178`](../../engine/app.py)):

```python
event_bus = EventBus(redis_url=settings.valkey_url)
await event_bus.connect()
ws_bridge = EventBusBridge(bus=event_bus, manager=ws_manager,
                           concurrency=settings.ws_event_bridge_concurrency)
ws_bridge.start()
```

On shutdown, `_shutdown()` stops the bridge, closes all WebSocket
connections (code 1000), and disconnects the bus — each in its own
exception guard so one failure does not block the rest.

### Consequences

- **Positive** — events emitted on any replica reach WS clients on
  every replica. No sticky sessions, no shared connection state.
- **Positive** — reuses the existing Valkey; no new infra.
- **Positive** — degrades gracefully: no Redis → in-process-only, logged
  at warning.
- **Negative** — the live `WebSocket` objects are still per-process.
  If replica A dies, its in-flight WS sessions drop and must reconnect.
  Event *correctness* does not depend on a single replica, but
  *connection continuity* does. See
  [`known-limitations.md`](../known-limitations.md) "WebSocket connection
  registry is process-local".
- **Negative** — the bridge fans out unconditionally: if a room has no
  local subscribers, the broadcast is still attempted. There is no
  back-pressure signal back to the `EventBus`. Under heavy event load
  with many idle rooms, this is wasted work.
- **Negative** — Redis pub/sub is at-most-once delivery. If a replica's
  bridge is down during a publish (e.g. mid-restart), that event is
  lost to that replica's clients. Acceptable for UI updates; would not
  be acceptable for order-state transitions (those are persisted to
  Postgres and the client re-syncs on reconnect).

## Pros and Cons of the Options

### Option 1 — Sticky sessions

- **Pros:** Simplest; no bridge code.
- **Cons:** Breaks the stateless-replica model; LB must inspect WS
  upgrade and maintain affinity; a replica restart drops all its
  pinned clients with no recovery to another replica; makes
  blue/green deploys harder.

### Option 2 — Shared connection registry + RPC

- **Pros:** Precise targeting (only the replica with the subscriber
  gets the message).
- **Cons:** Requires a consensus/registry layer; cross-replica RPC
  needs a transport (HTTP, gRPC) and retry semantics; significantly
  more moving parts than pub/sub fan-out.

### Option 3 — Redis/Valkey pub/sub (chosen)

- **Pros:** Reuses existing infra; fire-and-forget; trivial to reason
  about; graceful degradation.
- **Cons:** At-most-once; unconditional fan-out; per-process
  connections still drop on replica death.

### Option 4 — NATS / Kafka

- **Pros:** At-least-once delivery, consumer groups, persistence.
- **Cons:** A new infra dependency for a signal (UI updates) that
  does not need delivery guarantees beyond "best effort"; Kafka is
  operationally heavy for a single-tenant deploy. Revisit only if
  order-state realtime delivery requires durable guarantees.

## Links

- Original issue: SEV-275
- Source: [`engine/events/bus.py`](../../engine/events/bus.py),
  [`engine/api/ws/event_bridge.py`](../../engine/api/ws/event_bridge.py)
- Wiring: [`engine/app.py`](../../engine/app.py) lifespan
- Related ADR: [0005 — Valkey over Redis](0005-valkey-over-redis.md)
  (this bridge is one of the reasons Valkey stays in the stack)
- Known gap: [`docs/known-limitations.md`](../known-limitations.md)
  "WebSocket connection registry is process-local"
