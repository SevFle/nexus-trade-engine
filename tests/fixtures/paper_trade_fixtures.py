"""Shared fixtures for paper trade execution tests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from unittest.mock import MagicMock

import pytest

from engine.core.execution.paper_broker_interface import (
    PaperTradeBrokerConfig,
)


class _FakeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class _FakeCostBreakdown:
    slippage: Any = None


@dataclass
class _FakeOrder:
    id: str = "ord-1"
    symbol: str = "AAPL"
    quantity: int = 100
    side: _FakeSide = _FakeSide.BUY


def _make_cost(slippage_amount: float = 5.0):
    mock_cost = MagicMock()
    mock_cost.slippage = MagicMock()
    mock_cost.slippage.amount = slippage_amount
    return mock_cost


def _make_broker_config(**overrides: Any) -> PaperTradeBrokerConfig:
    defaults = {
        "fill_probability": 1.0,
        "partial_fill_enabled": False,
        "latency_ms": 0.0,
        "latency_jitter_ms": 0.0,
        "random_seed": 42,
    }
    defaults.update(overrides)
    return PaperTradeBrokerConfig(**defaults)


class _FakeDataProvider:
    def __init__(self, price: float = 150.0):
        self._price = price

    async def get_latest_price(self, symbol: str) -> float | None:
        return self._price


@pytest.fixture
def fake_order():
    return _FakeOrder()


@pytest.fixture
def fake_cost():
    return _make_cost()


@pytest.fixture
def broker_config():
    return _make_broker_config()


@pytest.fixture
def fake_data_provider():
    return _FakeDataProvider(price=150.0)
