"""
Tests for tax lot tracking with FIFO/LIFO selection, partial lot consumption,
holding period awareness, and wash sale detection.
"""

from datetime import UTC, datetime, timedelta

from engine.core.cost_model import DefaultCostModel, TaxLot, TaxMethod
from engine.core.portfolio import Portfolio


class TestPartialLotConsumption:
    """Test partial lot consumption - buy 100, sell 50."""

    def test_partial_lot_remaining(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0)
        p.close_position("AAPL", 50, 170.0)

        assert "AAPL" in p.positions
        assert p.positions["AAPL"].quantity == 50

    def test_partial_lot_cost_basis(self):
        p = Portfolio(initial_cash=100_000)
        p.open_position("AAPL", 100, 150.0)
        p.close_position("AAPL", 50, 170.0)

        remaining_lots = p.get_tax_lots("AAPL")
        assert len(remaining_lots) == 1
        assert remaining_lots[0].quantity == 50


class TestFIFO:
    """Test FIFO (First In, First Out) - oldest lots sold first."""

    def test_fifo_sells_oldest_first(self):
        p = Portfolio(initial_cash=100_000, tax_method=TaxMethod.FIFO)
        base_date = datetime.now(UTC) - timedelta(days=400)

        p.transaction_date = base_date - timedelta(days=500)
        p.open_position("AAPL", 50, 80.0)

        p.transaction_date = base_date - timedelta(days=100)
        p.open_position("AAPL", 50, 120.0)

        p.transaction_date = base_date
        consumed = p.close_position("AAPL", 50, 150.0)

        assert consumed[0]["purchase_price"] == 80.0


class TestLIFO:
    """Test LIFO (Last In, First Out) - newest lots sold first."""

    def test_lifo_sells_newest_first(self):
        p = Portfolio(initial_cash=100_000, tax_method=TaxMethod.LIFO)
        base_date = datetime.now(UTC) - timedelta(days=400)

        p.transaction_date = base_date - timedelta(days=300)
        p.open_position("AAPL", 50, 80.0)

        p.transaction_date = base_date - timedelta(days=100)
        p.open_position("AAPL", 50, 120.0)

        p.transaction_date = base_date
        consumed = p.close_position("AAPL", 50, 150.0)

        assert consumed[0]["purchase_price"] == 120.0
        assert consumed[0]["is_long_term"] is False


class TestHoldingPeriod:
    """Test short-term vs long-term holding period (365-day threshold)."""

    def test_short_term_loss_no_tax(self):
        cost_model = DefaultCostModel(short_term_tax_rate=0.37, long_term_tax_rate=0.20)

        now = datetime.now(UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=100,
                purchase_price=200.0,
                purchase_date=now - timedelta(days=30),
            )
        ]
        tax = cost_model.estimate_tax("AAPL", 150.0, 100, lots, TaxMethod.FIFO)
        assert tax.amount == 0.0

    def test_long_term_gain_at_reduced_rate(self):
        cost_model = DefaultCostModel(short_term_tax_rate=0.37, long_term_tax_rate=0.20)

        now = datetime.now(UTC)
        lots = [
            TaxLot(
                symbol="AAPL",
                quantity=100,
                purchase_price=100.0,
                purchase_date=now - timedelta(days=400),
            )
        ]
        tax = cost_model.estimate_tax("AAPL", 150.0, 100, lots, TaxMethod.FIFO)
        expected = (150.0 - 100.0) * 100 * 0.20
        assert abs(tax.amount - expected) < 1e-6


class TestWashSale:
    """Test wash sale detection and loss disallowance."""

    def test_wash_sale_loss_disallowed(self):
        cost_model = DefaultCostModel()

        sell_date = datetime.now(UTC)
        loss = -500.0
        buy_history = [
            {
                "symbol": "AAPL",
                "date": sell_date - timedelta(days=10),
                "price": 145.0,
                "quantity": 100,
            },
        ]

        result = cost_model.calculate_wash_sale_adjustment("AAPL", sell_date, loss, buy_history)

        assert result["is_wash_sale"] is True
        assert result["adjustment"] == 500.0

    def test_wash_sale_no_loss_no_adjustment(self):
        cost_model = DefaultCostModel()

        sell_date = datetime.now(UTC)
        gain = 500.0
        buy_history = [
            {
                "symbol": "AAPL",
                "date": sell_date - timedelta(days=10),
                "price": 145.0,
                "quantity": 100,
            },
        ]

        result = cost_model.calculate_wash_sale_adjustment("AAPL", sell_date, gain, buy_history)

        assert result["is_wash_sale"] is False

    def test_wash_sale_outside_window_no_disallowance(self):
        cost_model = DefaultCostModel()

        sell_date = datetime.now(UTC)
        loss = -500.0
        buy_history = [
            {
                "symbol": "AAPL",
                "date": sell_date - timedelta(days=60),
                "price": 145.0,
                "quantity": 100,
            },
        ]

        result = cost_model.calculate_wash_sale_adjustment("AAPL", sell_date, loss, buy_history)

        assert result["is_wash_sale"] is False


class TestPortfolioWashSale:
    """Test wash sale integration with portfolio."""

    def test_portfolio_wash_sale_disallows_loss(self):
        p = Portfolio(initial_cash=100_000)

        p.transaction_date = datetime.now(UTC) - timedelta(days=60)
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = datetime.now(UTC)
        p.close_position("AAPL", 100, 140.0, cost=5.0)

        p.transaction_date = datetime.now(UTC) + timedelta(days=5)
        p.open_position("AAPL", 100, 145.0)

        assert p.realized_pnl < 0
