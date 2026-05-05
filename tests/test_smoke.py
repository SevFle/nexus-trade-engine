"""Smoke test — verifies the test infrastructure collects coverage correctly."""

from __future__ import annotations


def test_nexus_sdk_importable():
    import nexus_sdk

    assert nexus_sdk.__version__ == "0.1.0"


def test_engine_importable():
    from engine.config import settings

    assert settings is not None


def test_core_types_instantiate():
    from nexus_sdk.types import CostBreakdown, Money, PortfolioSnapshot

    m = Money(amount=1.0)
    assert m.amount == 1.0
    cb = CostBreakdown(commission=m)
    assert cb.total.amount == 1.0
    snap = PortfolioSnapshot(cash=100.0, total_value=100.0)
    assert snap.cash == 100.0
