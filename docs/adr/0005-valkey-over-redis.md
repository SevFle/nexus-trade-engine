# ADR-0005: Valkey client + Valkey 8 broker (over redis-py / Redis)

- **Status**: Accepted
- **Date**: 2026-05-12
- **Deciders**: Lead maintainer + 1 reviewer
- **Tags**: broker, cache, infra, licensing

## Context and Problem Statement

The engine needs an in-memory data structure server for three
unrelated surfaces:

1. **Task queue broker** — TaskIQ holds work items and result backends
   here (see ADR-0004).
2. **Rate limiter** — fixed-window counters with millisecond TTLs,
   shared across API replicas.
3. **WebSocket pubsub** — fan-out of fill / alert / kill-switch
   events so every replica of the engine pushes to its connected
   subscribers.

Until 2024 the default answer was Redis. Redis Labs' 2024 license
change (RSALv2/SSPLv1) made "Redis 7.x and later" source-available
rather than OSS. Self-hosted users can still run it, but a project
that ships with `redis-server` as a default dependency is asking its
operators to track the licensing question on every upgrade.

Valkey is the Linux Foundation's BSD-3-Clause fork of Redis 7.2,
maintained by the original Redis core team contributors and shipped as
`valkey>=8` by every major Linux distro that dropped Redis over the
license change. Wire-protocol compatible with Redis 7; every existing
client (`redis-py`, `taskiq-redis`, `valkey-py`) speaks to it.

## Decision Drivers

- **OSS licensing.** The project is permissively licensed and intends
  to stay that way. Defaulting to an OSS-compatible broker removes a
  deployment-time licensing question for every operator.
- **Wire-protocol compatibility.** Anything we pick has to work with
  the existing Python async Redis ecosystem (`taskiq-redis`,
  `redis-py.asyncio`). Valkey does, by design.
- **One moving part.** Whichever server we pick has to serve all
  three surfaces above so operators only have to back up / monitor /
  secure one in-memory process.
- **Long-term support.** The distro story matters more than the
  vendor story; we want a server the operator can `apt install` or
  `dnf install` without subscribing to a vendor.

## Considered Options

1. **Valkey 8 + `valkey-py` (async)** — the LF fork plus its
   first-party async client. Drop-in wire-compatible with Redis.
2. **Redis 7+ + `redis-py`** — the original. Still works, but
   RSALv2/SSPLv1 means new releases are not OSI-OSS.
3. **DragonflyDB** — Redis-compatible, multithreaded, BSL. Higher
   throughput but a more restrictive license than Redis today.
4. **KeyDB** — Redis fork, MPL-2.0. Active development has slowed
   since Snap's acquisition; future uncertain.
5. **Memcached + a separate message bus** — splits "in-memory KV"
   from "pubsub/broker" across two servers. More operationally
   expensive than one server.

## Decision Outcome

Chosen option: **Option 1 — Valkey 8 + `valkey-py`**, because it
gives us the same wire protocol as Redis, the same client ergonomics
(`valkey.asyncio.Valkey` is a drop-in for `redis.asyncio.Redis`),
and a clear BSD-3-Clause license that lets every operator self-host
without a licensing conversation.

We standardise on the `valkey>=6` Python package; both that package
and `redis-py>=5` speak to a Valkey server, so the wire is
interchangeable. `engine/api/rate_limit.py` is intentionally written
to accept either client class so operators can swap if their
environment standardised on `redis-py` before this ADR.

The single Valkey instance is configured via `settings.valkey_url`
(default `valkey://localhost:6379/0`); see
[`engine/config.py`](../../engine/config.py) and the readiness check
in [`engine/api/routes/health.py`](../../engine/api/routes/health.py)
which `PING`s the broker on `/ready`.

### Consequences

- **Positive**
  - One in-memory server covers rate limiting, WebSocket pubsub, and
    the task queue. One backup, one secret, one TLS cert.
  - License stays permissive; no RSAL/SSPL conversation in the docs.
  - Operators who already run Redis 7 can point us at it without
    changes — the protocol is identical.
- **Negative**
  - Tooling ecosystem skews Redis-first; e.g. some dashboards ship
    Redis branding. Cosmetic, but visible.
  - Valkey is younger than Redis; the release cadence is fast but the
    long-term support story is shorter on history.
- **Neutral**
  - We pin both clients in `pyproject.toml` (`valkey>=6` for new
    code; `redis-py` remains a transitive of `taskiq-redis`) and use
    whichever is imported by the calling module. Don't mix them in
    the same module.

## Pros and Cons of the Options

### Option 1 — Valkey 8 + `valkey-py` (chosen)

- **Pros**
  - BSD-3-Clause server; no SSPL/RSAL conversation.
  - `valkey.asyncio.Valkey` is API-compatible with
    `redis.asyncio.Redis`. Calling code reads identically.
  - First-party Python client maintained alongside the server.
- **Cons**
  - Brand-new; ecosystem tooling is still catching up.
  - Operators with an existing Redis 7 deployment have to opt in to
    the swap (or just keep pointing us at Redis — the wire works).

### Option 2 — Redis 7+ + `redis-py`

- **Pros**
  - Default most operators expect.
  - Largest ecosystem of tooling, dashboards, hosted offerings.
- **Cons**
  - Server license (RSALv2 / SSPLv1) is not OSI-OSS. We'd be
    defaulting to a license that contradicts the project's own.
  - Vendors a single commercial entity; pricing / terms can change.

### Option 3 — DragonflyDB

- **Pros**
  - Multi-threaded; higher throughput per node than Redis/Valkey.
  - Wire-compatible with Redis 7.
- **Cons**
  - BSL license — same kind of source-available story that pushed us
    away from Redis.
  - Different operational profile (CPU-bound rather than I/O-bound);
    overkill for our traffic.

### Option 4 — KeyDB

- **Pros**
  - MPL-2.0 — OSI-OSS.
  - Multi-threaded.
- **Cons**
  - Development pace has slowed post-acquisition.
  - Smaller deployment footprint; less testing.

### Option 5 — Memcached + a separate message bus

- **Pros**
  - Memcached is the simplest in-memory KV; very well understood.
- **Cons**
  - Two services to run, monitor, back up, and secure.
  - Memcached has no pubsub; we'd need a separate bus (NATS, MQTT,
    etc.).

## Links

- Related code:
  - [`engine/app.py`](../../engine/app.py) — constructs the
    application-scoped Valkey client.
  - [`engine/api/rate_limit.py`](../../engine/api/rate_limit.py) —
    note the dual `Valkey | Redis` type hint.
  - [`engine/events/bus.py`](../../engine/events/bus.py) — Redis
    pub/sub for cross-replica event delivery, consumed by the
    [`EventBusBridge`](../../engine/api/ws/event_bridge.py) to fan out
    to WebSocket rooms.
  - [`engine/tasks/worker.py`](../../engine/tasks/worker.py) —
    `taskiq-redis` broker against Valkey.
- Related ADRs:
  - [ADR-0004 — TaskIQ over Celery / RQ / arq](0004-task-queue-taskiq.md).
- Supersedes: —
- Superseded by: —
- External references:
  - [Valkey](https://valkey.io/)
  - [Redis licensing change
    (2024-03-20)](https://redis.com/blog/redis-adopts-dual-source-available-licensing/)
  - [Linux Foundation announces
    Valkey](https://www.linuxfoundation.org/press/announcing-valkey)
