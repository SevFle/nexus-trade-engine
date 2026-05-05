from __future__ import annotations

from nexus_sdk import Side

from engine.observability.metrics import NullBackend, get_metrics, set_metrics


def test_engine_import_and_call():
    backend = NullBackend()
    set_metrics(backend)
    assert get_metrics() is backend
    backend.counter("canary")
    set_metrics(NullBackend())


def test_nexus_sdk_import_and_call():
    assert Side.BUY.value == "buy"
    assert Side.SELL.value == "sell"
