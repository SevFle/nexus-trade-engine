"""Retry and rate-limit primitives shared by all adapters.

Kept dependency-free (asyncio + structlog) so adapters stay focused on
provider-specific I/O.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import TYPE_CHECKING, TypeVar

import structlog

from engine.data.providers.base import (
    FatalProviderError,
    RateLimit,
    TransientProviderError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = structlog.get_logger()

T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY_S = 0.25
DEFAULT_MAX_DELAY_S = 8.0


class TokenBucket:
    """Per-process token bucket. Thread-safe within a single asyncio loop.

    Used by every adapter to throttle outbound calls below provider quotas.
    ``capacity = 0`` disables limiting (used by free providers without quota).
    """

    def __init__(self, rate_limit: RateLimit) -> None:
        per_minute = max(0, rate_limit.requests_per_minute)
        self._capacity = max(rate_limit.burst, 1) if per_minute else 0
        self._refill_per_second = per_minute / 60.0 if per_minute else 0.0
        self._tokens = float(self._capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._capacity == 0:
            return

        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated
                self._tokens = min(
                    float(self._capacity),
                    self._tokens + elapsed * self._refill_per_second,
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._refill_per_second
                await asyncio.sleep(wait)


async def call_with_retry(
    func: Callable[[], Awaitable[T]],
    *,
    provider: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    max_delay_s: float = DEFAULT_MAX_DELAY_S,
) -> T:
    """Invoke ``func`` with exponential backoff + jitter on transient failures.

    Fatal errors propagate immediately. Last attempt's exception is re-raised
    so callers can decide whether to fail-over to the next provider.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await func()
        except FatalProviderError:
            raise
        except (TransientProviderError, TimeoutError) as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            delay = min(max_delay_s, base_delay_s * (2 ** (attempt - 1)))
            jitter = random.uniform(0, delay * 0.25)  # noqa: S311
            await asyncio.sleep(delay + jitter)
            logger.warning(
                "data_provider.retry",
                provider=provider,
                attempt=attempt,
                delay=delay,
                error=str(exc),
            )
    assert last_exc is not None
    raise last_exc
