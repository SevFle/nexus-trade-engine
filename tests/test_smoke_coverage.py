from __future__ import annotations

import importlib.metadata

import pytest


def test_nexus_sdk_importable():
    import nexus_sdk

    expected = importlib.metadata.version("nexus-trade-engine")
    assert nexus_sdk.__version__ == expected


def test_nexus_sdk_core_types():
    from nexus_sdk import Money, Side, Signal

    assert Side.BUY.value == "buy"
    money = Money(amount=100.0, currency="USD")
    assert money.amount == pytest.approx(100.0)
    sig = Signal(side=Side.BUY, symbol="AAPL", strategy_id="test")
    assert sig.symbol == "AAPL"
