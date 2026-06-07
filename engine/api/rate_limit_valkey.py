"""Distributed token-bucket backend backed by Valkey/Redis.

For single-pod deployments the in-memory backend in
``engine/api/rate_limit.py`` is sufficient, but as soon as the API runs
behind more than one worker process the per-pod buckets diverge and the
effective global limit becomes ``per_minute * pod_count``. This backend
keeps the bucket state in Valkey so all pods share one bucket per key.

Atomicity
---------
The refill + consume sequence is implemented as a single Lua script so
two concurrent increments (e.g. two pods handling two requests for the
same user at the same instant) cannot interleave. Redis/Valkey evaluates
Lua scripts atomically: while the script runs no other command runs on
the same connection (and, in a single-threaded deployment, on the entire
server).

Clock
-----
The script reads ``redis.call('TIME')`` to source ``now`` so all pods
agree on a clock. This avoids the classic distributed-token-bucket
pitfall where wall-clock skew across nodes causes negative ``elapsed``
values and an unintentional refill reset.

Failure modes
-------------
- If Valkey is briefly unreachable, ``update`` raises ``ValkeyError``;
  the middleware catches it and lets the request through (fail-open),
  surfacing a counter so operators can alert on it. The alternative
  (fail-closed) would let a Valkey outage take the entire API offline,
  which is worse than temporarily allowing a few extra requests.
- The Lua script does not run against a cluster-hash-tagged key; for
  Redis Cluster deployments the caller should ensure the key is already
  slotted correctly (the default ``ratelimit:<key>`` prefix is fine for
  non-clustered Valkey).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from engine.api.rate_limit import _MAX_RETRY_AFTER_SEC, _MIN_RETRY_AFTER_SEC

if TYPE_CHECKING:
    from valkey.asyncio import Valkey


# Expected element count of the Lua script's return array
# (see _TOKEN_BUCKET_LUA): [ok, remaining, retry].
_LUA_RESULT_LEN = 3


# Lua script implementing the token bucket atomically.
#
# Returns a 3-element array:
#   [1] = 1 if a token was consumed, 0 if rate-limited
#   [2] = remaining tokens (integer floor) for the X-RateLimit-Remaining header
#   [3] = seconds to wait before retrying (decimal string) clamped to the
#         same limits as the in-memory backend
#
# ARGV:
#   [1] = capacity (max burst)
#   [2] = refill_per_sec
#   [3] = ttl (seconds — bucket state is dropped after this much idle
#         time, which both bounds Valkey memory and gracefully expires
#         idle users)
_TOKEN_BUCKET_LUA = """
local capacity = tonumber(ARGV[1])
local refill_per_sec = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) + tonumber(now_parts[2]) / 1e6

local state = redis.call('HMGET', KEYS[1], 'tokens', 'last')
local tokens, last
if state[1] == false then
    tokens = capacity
    last = now
else
    tokens = tonumber(state[1])
    last = tonumber(state[2])
end

-- Clock skew defence: if a prior write came from a node whose clock
-- was ahead of ours, ``elapsed`` would be negative and we'd silently
-- reset the bucket. Clamp at zero so the worst case is "no refill
-- this turn".
local elapsed = 0
if now > last then
    elapsed = now - last
end
tokens = math.min(capacity, tokens + elapsed * refill_per_sec)

local ok = 0
local retry = '0'
if tokens >= 1.0 then
    tokens = tokens - 1.0
    ok = 1
elseif refill_per_sec > 0 then
    local deficit = 1.0 - tokens
    local r = deficit / refill_per_sec
    if r < 0.001 then r = 0.001 end
    if r > 86400 then r = 86400 end
    retry = tostring(r)
else
    retry = '86400'
end

redis.call('HMSET', KEYS[1], 'tokens', tostring(tokens), 'last', tostring(now))
redis.call('EXPIRE', KEYS[1], ttl)
return {ok, tostring(math.floor(tokens)), retry}
""".strip()


def _coerce_result(raw: Any) -> tuple[bool, int, float]:
    """Translate the Lua return value into the backend Protocol shape.

    Accepts bytes/str/int from either the real Valkey client or
    fakeredis, and normalises the retry_after to a float.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != _LUA_RESULT_LEN:
        msg = f"unexpected Lua result shape: {raw!r}"
        raise RuntimeError(msg)
    ok_raw, remaining_raw, retry_raw = raw

    def _to_int(v: Any) -> int:
        if isinstance(v, bytes):
            v = v.decode("ascii")
        return int(float(v))

    def _to_float(v: Any) -> float:
        if isinstance(v, bytes):
            v = v.decode("ascii")
        return float(v)

    ok = _to_int(ok_raw) == 1
    remaining = _to_int(remaining_raw)
    retry = _to_float(retry_raw)
    # Clamp to the same bounds as the in-memory backend so callers see
    # consistent header values regardless of which backend is in use.
    retry = max(_MIN_RETRY_AFTER_SEC, min(retry, _MAX_RETRY_AFTER_SEC))
    return ok, remaining, retry


class ValkeyBucketBackend:
    """Atomic Valkey-backed token bucket store.

    Suitable for multi-pod deployments. ``client`` must be an
    ``valkey.asyncio.Valkey`` (or any compatible async client such as
    ``fakeredis.FakeAsyncRedis``) and is owned by the caller — the
    backend does not open or close it.

    ``key_prefix`` is prepended to every bucket key to namespace the
    rate-limit keys from other Valkey usage. Defaults to ``"ratelimit:"``.

    ``state_ttl_sec`` controls how long bucket state survives after the
    last consume. Setting it well above the configured ``per_minute``
    window is safe: an idle user simply re-gets a full bucket on next
    request. Setting it too low (e.g. < 60s) would defeat the limiter
    by expiring state before the window rolls over.
    """

    def __init__(
        self,
        client: Valkey,
        *,
        key_prefix: str = "ratelimit:",
        state_ttl_sec: int = 600,
    ) -> None:
        self._client = client
        self._prefix = key_prefix
        self._ttl = state_ttl_sec
        # register_script amortises EVALSHA across calls (falls back to
        # EVAL the first time and after a SCRIPT FLUSH).
        self._script = client.register_script(_TOKEN_BUCKET_LUA)

    async def update(
        self,
        key: str,
        capacity: int,
        refill_per_sec: float,
        now: float,  # noqa: ARG002 — accepted for Protocol parity; the Lua script sources its own clock
    ) -> tuple[bool, int, float]:
        full_key = f"{self._prefix}{key}"
        # The Lua script reads TIME on the server so we don't need the
        # caller's monotonic value here. The Protocol accepts it for
        # parity with InMemoryBucketBackend.
        raw = await self._script(
            keys=[full_key],
            args=[
                str(capacity),
                str(refill_per_sec),
                str(self._ttl),
            ],
        )
        return _coerce_result(raw)

    async def reset(self, key: str) -> None:
        """Drop bucket state for ``key``. Mainly used by tests."""
        await self._client.delete(f"{self._prefix}{key}")


__all__ = ["ValkeyBucketBackend"]
