"""Worker heartbeat: keep ``worker:heartbeat`` fresh in Valkey.

A background asyncio loop running inside the worker process refreshes the
``worker:heartbeat`` key in the shared Valkey instance every
:data:`DEFAULT_INTERVAL` seconds. The docker-compose healthcheck reads
that key back and fails if it is older than :data:`DEFAULT_MAX_AGE`,
turning the previous "can the worker reach Valkey?" probe into a true "is
the worker event loop still alive?" probe.

Why epoch seconds (and not ISO 8601)?
    The reader and the writer share a single container clock, so comparing
    ``time.time() - float(value)`` is exact and free of timezone-parsing
    ambiguity. The value is still trivially human-readable
    (``date -d @<value>``).

Import safety
    Importing this module never opens a connection. The writing side is
    driven by :func:`heartbeat_loop` — spawned from the taskiq
    ``WORKER_STARTUP`` hook via :func:`start_heartbeat` — and the reading
    side is exposed as ``python -m engine.tasks.heartbeat check`` plus the
    pure :func:`is_fresh` helper, so it is unit-testable without a broker.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import math
import os
import time
from urllib.parse import urlparse, urlunparse

import redis
import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger()

#: Valkey key holding the worker's last-seen epoch timestamp.
HEARTBEAT_KEY = "worker:heartbeat"
#: How often (seconds) the loop refreshes :data:`HEARTBEAT_KEY`.
DEFAULT_INTERVAL = 15.0
#: Max age (seconds) at which the key is still considered "fresh". 3x the
#: interval tolerates a couple of missed beats (restart, GC pause, slow
#: write) before the healthcheck flips unhealthy.
DEFAULT_MAX_AGE = 45.0

#: ``redis-py`` only recognises the ``redis://`` / ``rediss://`` schemes even
#: though it speaks the RESP wire protocol that Valkey also implements. The
#: configured ``valkey://`` / ``valkeys://`` URL is therefore normalised to
#: the scheme ``redis-py`` accepts. Unknown schemes are passed through so a
#: genuine misconfiguration surfaces as a ``redis-py`` error rather than a
#: silent rewrite.
_SCHEME_ALIASES: dict[str, str] = {
    "redis": "redis",
    "rediss": "rediss",
    "valkey": "redis",
    "valkeys": "rediss",
}

# Holds the currently running heartbeat task + its client so the shutdown
# hook can cancel and close them deterministically. Populated by
# :func:`start_heartbeat`, cleared by :func:`stop_heartbeat`.
_active: dict[str, object] = {}


def _normalize_url(url: str) -> str:
    """Rewrite a ``valkey(s)://`` URL to the ``redis(s)://`` form redis-py needs."""
    parsed = urlparse(url)
    scheme = _SCHEME_ALIASES.get(parsed.scheme.lower())
    if scheme is None:
        return url
    return urlunparse(parsed._replace(scheme=scheme))


def _resolve_url(url: str | None) -> str:
    """Resolve the configured Valkey URL, falling back to the env var / default."""
    if url is None:
        url = os.environ.get("NEXUS_VALKEY_URL", "valkey://localhost:6379/0")
    return url


def make_async_client(url: str | None = None, **kwargs):  # type: ignore[no-untyped-def]
    """Build an async redis client wired to the configured Valkey URL."""
    return aioredis.from_url(
        _normalize_url(_resolve_url(url)),
        decode_responses=True,
        **kwargs,
    )


def make_sync_client(url: str | None = None, **kwargs):  # type: ignore[no-untyped-def]
    """Build a sync redis client wired to the configured Valkey URL."""
    return redis.from_url(
        _normalize_url(_resolve_url(url)),
        decode_responses=True,
        **kwargs,
    )


def _format_now(clock=time.time) -> str:
    return f"{clock():.6f}"


async def write_heartbeat(client, *, key: str = HEARTBEAT_KEY, clock=time.time) -> str:
    """Write the current epoch to ``key`` and return the value written."""
    value = _format_now(clock)
    await client.set(key, value)
    return value


async def heartbeat_loop(
    client,
    *,
    interval: float = DEFAULT_INTERVAL,
    key: str = HEARTBEAT_KEY,
    clock=time.time,
    sleep=asyncio.sleep,
) -> None:
    """Refresh ``key`` every ``interval`` seconds until cancelled.

    Writes immediately on entry so the key exists before the first interval
    elapses, then re-writes every ``interval`` seconds. ``clock`` and
    ``sleep`` are injectable so tests can drive iterations and timestamps
    without real delays. Errors writing the heartbeat are logged and
    swallowed — a transient Valkey blip must not kill the loop (the
    healthcheck will flag a stale key on its own). The loop exits cleanly
    on ``CancelledError``.
    """
    while True:
        try:
            await client.set(key, _format_now(clock))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "tasks.heartbeat.write_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
        await sleep(interval)


def is_fresh(stored, *, now: float | None = None, max_age: float = DEFAULT_MAX_AGE) -> bool:
    """Return ``True`` iff ``stored`` parses to a timestamp within ``max_age``.

    Pure and side-effect free, so it can be unit-tested without a broker.
    ``stored`` may be ``None`` (key absent), :class:`bytes`, or a
    :class:`str`; anything that does not parse to a finite float — or whose
    age exceeds ``max_age`` — is treated as stale. A future timestamp
    (clock skew or a manual set) still counts as fresh.
    """
    if stored is None:
        return False
    if isinstance(stored, bytes):
        try:
            stored = stored.decode()
        except UnicodeDecodeError:
            return False
    try:
        ts = float(stored)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(ts):
        return False
    current = time.time() if now is None else now
    return (current - ts) <= max_age


def check_once(
    *,
    url: str | None = None,
    max_age: float = DEFAULT_MAX_AGE,
    key: str = HEARTBEAT_KEY,
    client=None,
) -> bool:
    """Read the heartbeat key once and report its freshness (sync).

    Used by the docker-compose worker healthcheck
    (``python -m engine.tasks.heartbeat check``) — the distroless runtime
    image ships no shell and no ``valkey-cli``, so we read the same
    ``worker:heartbeat`` key through the embedded ``redis-py`` instead.
    """
    own_client = client is None
    if client is None:
        client = make_sync_client(url)
    try:
        stored = client.get(key)
    finally:
        if own_client:
            with contextlib.suppress(Exception):
                client.close()
    return is_fresh(stored, max_age=max_age)


def start_heartbeat(
    *,
    interval: float = DEFAULT_INTERVAL,
    url: str | None = None,
    client=None,
) -> asyncio.Task | None:
    """Spawn :func:`heartbeat_loop` as a background task (idempotent).

    Intended to be awaited-from-side-effect by the taskiq
    ``WORKER_STARTUP`` hook so the heartbeat genuinely reflects worker
    event-loop liveness rather than that of a sidecar process. Returns the
    spawned task, or ``None`` when there is no running event loop (e.g.
    import time) so it is always safe to call.
    """
    existing = _active.get("task")
    if existing is not None and not existing.done():
        return existing  # type: ignore[return-value]
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if client is None:
        client = make_async_client(url)
    task = loop.create_task(
        heartbeat_loop(client, interval=interval),
        name="worker-heartbeat",
    )
    _active["task"] = task
    _active["client"] = client
    logger.info("tasks.heartbeat.started", interval=interval, key=HEARTBEAT_KEY)
    return task


async def stop_heartbeat() -> None:
    """Cancel the heartbeat task and close its client (idempotent)."""
    task = _active.pop("task", None)
    client = _active.pop("client", None)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    if client is not None:
        with contextlib.suppress(Exception):
            await client.aclose()


async def _run_forever(*, interval: float) -> None:
    """Standalone foreground runner for ``python -m ... run``."""
    client = make_async_client()
    try:
        await heartbeat_loop(client, interval=interval)
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``run`` writes forever, ``check`` reports freshness."""
    parser = argparse.ArgumentParser(
        prog="engine.tasks.heartbeat",
        description="Worker heartbeat writer / liveness checker.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Refresh the heartbeat key every interval (foreground).")
    run_p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)

    check_p = sub.add_parser("check", help="Exit 0 if the heartbeat is fresh, 1 otherwise.")
    check_p.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE)

    args = parser.parse_args(argv)

    if args.command == "check":
        return 0 if check_once(max_age=args.max_age) else 1
    if args.command == "run":
        try:
            asyncio.run(_run_forever(interval=args.interval))
        except KeyboardInterrupt:
            return 0
        return 0
    return 2  # pragma: no cover - argparse enforces a subcommand


if __name__ == "__main__":  # pragma: no cover - exercised via `python -m`
    raise SystemExit(main())
