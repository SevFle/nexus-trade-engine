"""Broker integration surface (gh#136).

Operators register a :class:`BrokerAdapter` per broker (Alpaca,
IBKR, Binance, Kraken, Oanda, …); the live-trading loop (gh#109)
calls :meth:`BrokerAdapter.submit` and :meth:`BrokerAdapter.cancel`
and consumes :meth:`BrokerAdapter.events` to feed the OMS state
machine.

This module pins only the Protocol + registry. Concrete adapter
implementations live under their own subpackages — those are the
broker-by-broker integration tests gh#136 will exercise.

Public surface:
- :class:`BrokerAdapter` Protocol — async submit/cancel/events.
- :class:`BrokerError` and concrete subtypes for failure modes the
  OMS knows how to react to.
- :func:`register_broker` / :func:`get_broker` / :func:`list_brokers`
  — operator-facing registry.
"""

from engine.core.brokers.base import (
    BrokerAdapter,
    BrokerAuthError,
    BrokerConnectionError,
    BrokerError,
    BrokerRejectError,
    SubmittedOrder,
)
from engine.core.brokers.registry import (
    get_broker,
    list_brokers,
    register_broker,
)

__all__ = [
    "BrokerAdapter",
    "BrokerAuthError",
    "BrokerConnectionError",
    "BrokerError",
    "BrokerRejectError",
    "SubmittedOrder",
    "get_broker",
    "list_brokers",
    "register_broker",
]
