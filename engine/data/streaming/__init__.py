"""Real-time streaming primitives (gh#133).

Today this exposes:

- :class:`BoundedBuffer` — fixed-capacity buffer with a drop policy.
  Use it between a fast producer (market data feed) and a slower
  consumer (strategy / WebSocket fan-out) so a stalled consumer
  does not block the producer or grow memory unbounded.
- :class:`ReplayLog` — bounded recent-history log. Useful for
  resubscribe / catch-up flows where a new consumer joins mid-stream
  and needs the last N messages.

Out of scope (explicit follow-ups):
- Wiring this into the actual market-data provider abstraction
  (``engine/data/providers/``) and the WebSocket fan-out
  (``engine/api/websocket/``).
- Multi-consumer fan-out across replicas — that's the Redis/Valkey
  pubsub work tracked under gh#7 follow-ups.
- Persisted replay across restarts.
"""

from engine.data.streaming.replay import ReplayLog
from engine.data.streaming.ring_buffer import BoundedBuffer, DropPolicy

__all__ = ["BoundedBuffer", "DropPolicy", "ReplayLog"]
