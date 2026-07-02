"""Canonical Taskiq broker definition for the Nexus task queue.

This module owns the single shared :class:`taskiq_redis.ListQueueBroker`
(a Redis/Valkey-backed broker) that every other part of the codebase
re-uses:

* :mod:`engine.tasks.worker` re-exports ``broker`` / ``scheduler`` from
  here and registers the legacy ``run_backtest_task`` on it.
* :mod:`engine.tasks.definitions` registers its tasks on this broker.
* :mod:`engine.app` wires ``await broker.startup()`` /
  ``await broker.shutdown()`` into the FastAPI app factory lifespan so the
  web/API process opens and closes the broker's Redis connection pool in
  lock-step with the rest of the application lifecycle.

The broker URL is derived from :attr:`settings.valkey_url` by swapping the
``valkey://`` scheme for ``redis://``: :mod:`taskiq_redis` speaks the Redis
(RESP) wire protocol, which Valkey implements, so the two are
interchangeable at the protocol level. The same Valkey instance is shared
with caching, rate-limiting and the event bus, so a single URL drives every
subsystem.

Keeping the construction in its own module (rather than in ``worker.py``)
means the web/API process can import and lifecycle-manage the broker
without dragging in the worker's task definitions — and their heavy
backtest-engine imports — at module load time.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

import structlog
from taskiq import TaskiqScheduler
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

from engine.config import settings
from engine.observability.taskiq_middleware import CorrelationMiddleware

logger = structlog.get_logger()

# ``taskiq_redis`` only recognises the ``redis://`` / ``rediss://`` schemes
# even though it speaks the Redis wire protocol (RESP), which Valkey also
# implements. The configured ``valkey://`` / ``valkeys://`` URL (shared with
# the rest of the app — caching, rate limiting, the event bus) is therefore
# normalised to the scheme ``taskiq_redis`` accepts. Only the scheme token
# changes; host, port and database path are passed through untouched.
# Unknown schemes are rejected so a misconfigured URL fails loudly at
# construction time rather than silently producing a broker that can never
# connect.
_SCHEME_ALIASES: dict[str, str] = {
    "redis": "redis",
    "rediss": "rediss",
    "valkey": "redis",
    "valkeys": "rediss",
}


def _normalize_broker_url(url: str) -> str:
    """Translate a Redis/Valkey URL into the scheme ``taskiq_redis`` expects.

    :param url: a URL using one of the supported input schemes
        (``redis://``, ``rediss://``, ``valkey://`` or ``valkeys://``).
    :returns: the same URL with its scheme rewritten to ``redis://`` or
        ``rediss://`` as appropriate; the host/port/path are unchanged.
    :raises ValueError: if the URL's scheme is not recognised.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in _SCHEME_ALIASES:
        raise ValueError(
            f"Unsupported broker URL scheme {scheme!r}; expected one of "
            f"redis://, rediss://, valkey:// or valkeys:// (url={url!r})"
        )
    return urlunparse(parsed._replace(scheme=_SCHEME_ALIASES[scheme]))


def build_broker(url: str | None = None) -> ListQueueBroker:
    """Construct the shared broker wired to a Redis/Valkey instance.

    Factored as a function so the wiring is unit-testable without a live
    broker: tests can call ``build_broker("redis://example:6379/0")`` and
    assert on the resolved URL, the attached result backend and the
    correlation middleware. The fluent ``with_*`` builders return the same
    broker instance (``Self``), so the returned object still exposes
    ``startup()`` / ``shutdown()`` for lifespan wiring.

    The scheme is normalised via :func:`_normalize_broker_url`:
    ``valkey://`` and ``valkeys://`` (the schemes used elsewhere in the app)
    are mapped onto the ``redis://`` / ``rediss://`` schemes that
    ``taskiq_redis`` understands, and any other scheme raises
    ``ValueError``.

    :param url: Redis/Valkey URL. When ``None`` (the default) the broker is
        wired to :attr:`settings.valkey_url`, resolved inside the body so
        the module's import-time construction stays decoupled from settings
        resolution and tests can drive :func:`build_broker` with explicit
        URLs.
    :returns: a fully wired :class:`ListQueueBroker` ready for
        ``await broker.startup()``.
    """
    if url is None:
        url = settings.valkey_url
    url = _normalize_broker_url(url)
    return (
        ListQueueBroker(url=url)
        .with_result_backend(RedisAsyncResultBackend(redis_url=url))
        .with_middlewares(CorrelationMiddleware())
    )


# Module-level mirror of the normalised configured URL, kept for backwards
# compatibility with anything importing ``broker_url`` directly (it is part
# of ``__all__``). Derived purely from settings so it tracks the deployment
# configuration without opening a connection.
broker_url: str = _normalize_broker_url(settings.valkey_url)


# The single shared broker instance. Importing this module does NOT open a
# connection — the Redis connection pool is only created on
# ``await broker.startup()``, which the FastAPI lifespan invokes on app
# boot (see :func:`engine.app.lifespan`). Tasks registered anywhere via
# ``@broker.task`` therefore land on this same object, and a single
# ``await broker.shutdown()`` tears the pool down.
broker: ListQueueBroker = build_broker()

# A scheduler is required by the taskiq worker process to drive scheduled
# (cron-like) tasks. There are none registered yet, but keeping it here
# preserves the previous ``engine.tasks.worker`` surface and gives future
# scheduled tasks a ready-made home. ``sources`` is intentionally empty.
scheduler = TaskiqScheduler(broker=broker, sources=[])


__all__ = ["broker", "broker_url", "build_broker", "scheduler"]
