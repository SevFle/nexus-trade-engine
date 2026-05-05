"""Canary test — verifies pytest-cov actually measures nexus_sdk coverage."""
from __future__ import annotations

import pytest

import coverage

from nexus_sdk import Money, Side, Signal


def test_canary_import_nexus_sdk():
    assert Money(amount=1.0, currency="USD").amount == 1.0
    assert Side.BUY.value == "buy"
    assert Signal.buy("AAPL", strategy_id="canary").symbol == "AAPL"


def test_canary_coverage_measurement_active():
    cov = coverage.Coverage.current()
    if cov is None:
        pytest.skip("Coverage not active — run with --cov enabled")
    data = cov.get_data()
    measured_files = data.measured_files()
    sdk_files = [f for f in measured_files if "nexus_sdk" in f]
    assert len(sdk_files) > 0, (
        "nexus_sdk not in measured files — check [tool.coverage.run] source"
    )
