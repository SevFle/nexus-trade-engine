from __future__ import annotations


def test_nexus_sdk_importable():
    import nexus_sdk

    assert nexus_sdk.__version__ == "0.1.0"


def test_nexus_sdk_core_types():
    from nexus_sdk import Money, Side, Signal

    assert Side.BUY is not None
    money = Money(amount=100.0, currency="USD")
    assert money.amount == 100.0
    sig = Signal(side=Side.BUY, symbol="AAPL", strategy_id="test")
    assert sig.symbol == "AAPL"
