"""Prometheus metrics for the WebSocket API (SEV-275).

Six declared series, exposed via the existing
:class:`~engine.observability.prometheus.PrometheusBackend` (or any
other :class:`~engine.observability.metrics.MetricsBackend`):

- ``ws_connections`` (gauge)            — currently open connections
- ``ws_subscriptions`` (gauge)          — currently active subscriptions
- ``ws_messages_sent_total`` (counter)  — frames successfully delivered
- ``ws_messages_dropped_total`` (counter) — frames dropped (slow consumer,
                                            rate limited, send failure)
- ``ws_bridge_lag_seconds`` (gauge)     — pubsub → client latency
- ``ws_auth_failures_total`` (counter)  — failed handshake attempts

Tags ``family``, ``reason`` etc. follow the same lowercase-dotted
convention as the rest of the engine. The module also exposes
:class:`WSNames` so callers don't have to remember the exact
strings.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.observability.metrics import get_metrics


@dataclass(frozen=True)
class _Names:
    connections: str = "ws.connections"
    subscriptions: str = "ws.subscriptions"
    messages_sent: str = "ws.messages_sent_total"
    messages_dropped: str = "ws.messages_dropped_total"
    bridge_lag: str = "ws.bridge_lag_seconds"
    auth_failures: str = "ws.auth_failures_total"
    bridge_errors: str = "ws.bridge_errors_total"
    dead_letter: str = "ws.dead_letter_total"


names = _Names()


# ---------------------------------------------------------------------------
# Thin helpers — centralises the tag dicts so callers don't drift.
# ---------------------------------------------------------------------------
def connection_opened() -> None:
    get_metrics().gauge(names.connections, _gauge_delta(+1))  # type: ignore[arg-type]
    # gauges are absolute; for open/close we use absolute reads via
    # the snapshot function below. Keep this as a counter for tests
    # that observe the call count directly.


def connection_closed() -> None:
    get_metrics().counter(names.connections, -1.0)


def set_connections(value: int) -> None:
    get_metrics().gauge(names.connections, float(value))


def set_subscriptions(value: int) -> None:
    get_metrics().gauge(names.subscriptions, float(value))


def message_sent(*, family: str = "") -> None:
    get_metrics().counter(
        names.messages_sent, tags={"family": family} if family else None
    )


def message_dropped(*, reason: str, family: str = "") -> None:
    tags: dict[str, str] = {"reason": reason}
    if family:
        tags["family"] = family
    get_metrics().counter(names.messages_dropped, tags=tags)


def bridge_lag(seconds: float) -> None:
    get_metrics().gauge(names.bridge_lag, seconds)


def auth_failure(*, reason: str) -> None:
    get_metrics().counter(names.auth_failures, tags={"reason": reason})


def bridge_error(*, reason: str) -> None:
    get_metrics().counter(names.bridge_errors, tags={"reason": reason})


def dead_letter(*, reason: str) -> None:
    get_metrics().counter(names.dead_letter, tags={"reason": reason})


def _gauge_delta(_v: int) -> float:  # pragma: no cover - placeholder
    return float(_v)


__all__ = [
    "auth_failure",
    "bridge_error",
    "bridge_lag",
    "connection_closed",
    "connection_opened",
    "dead_letter",
    "message_dropped",
    "message_sent",
    "names",
    "set_connections",
    "set_subscriptions",
]
