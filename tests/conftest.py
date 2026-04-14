"""
Shared test fixtures.
"""

import sys
from pathlib import Path

import pytest

# Add engine to Python path so tests can import engine modules
sys.path.insert(0, str(Path(__file__).parent.parent / "engine"))


@pytest.fixture
def sample_portfolio():
    from core.portfolio import Portfolio
    p = Portfolio(initial_cash=100_000.0, name="Test Portfolio")
    return p


@pytest.fixture
def default_cost_model():
    from core.cost_model import DefaultCostModel
    return DefaultCostModel(
        commission_per_trade=1.0,
        spread_bps=5.0,
        slippage_bps=10.0,
    )


@pytest.fixture
def sample_market_state():
    from plugins.sdk import MarketState
    bars = [{"open": 148 + i, "high": 152 + i, "low": 147 + i, "close": 150 + i, "volume": 1000000} for i in range(60)]
    return MarketState(
        prices={"AAPL": 150.0, "MSFT": 420.0, "GOOGL": 175.0},
        volumes={"AAPL": 50_000_000, "MSFT": 25_000_000, "GOOGL": 20_000_000},
        ohlcv={"AAPL": bars, "MSFT": bars, "GOOGL": bars},
    )
