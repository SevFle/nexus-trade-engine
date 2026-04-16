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
        tax = cost_model.estimate_tax("AAPL", 150.0, 100, lots, TaxMethod.FIFO, sell_date=now)
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

    def test_wash_sale_return_shape_consistent(self):
        cost_model = DefaultCostModel()
        sell_date = datetime.now(UTC)
        no_wash = cost_model.calculate_wash_sale_adjustment("AAPL", sell_date, 100.0, [])
        assert set(no_wash.keys()) == {
            "is_wash_sale",
            "adjustment",
            "adjustment_per_share",
            "replacement_lots",
        }
        assert no_wash["replacement_lots"] == []

        wash = cost_model.calculate_wash_sale_adjustment(
            "AAPL",
            sell_date,
            -100.0,
            [
                {
                    "symbol": "AAPL",
                    "date": sell_date - timedelta(days=5),
                    "price": 100.0,
                    "quantity": 10,
                }
            ],
        )
        assert set(wash.keys()) == {
            "is_wash_sale",
            "adjustment",
            "adjustment_per_share",
            "replacement_lots",
        }


class TestPortfolioWashSale:
    """Test wash sale integration with portfolio.

    IRS rule: buy within 30 days of a loss sale → loss disallowed,
    added to replacement lot cost basis.
    """

    def test_portfolio_wash_sale_adjusts_replacement_lot_cost_basis(self):
        p = Portfolio(initial_cash=200_000)

        # Day 0: Buy 100 shares at $150
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        # Day 60: Sell 100 shares at $140 → $1000 loss
        p.transaction_date = datetime(2026, 3, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 140.0, cost=0.0)
        assert p.realized_pnl < 0

        # Day 65 (5 days later, within 30-day window): Buy 100 shares at $145
        p.transaction_date = datetime(2026, 3, 6, tzinfo=UTC)
        p.open_position("AAPL", 100, 145.0)

        # The replacement lot should have its cost basis adjusted upward
        # by the disallowed loss ($1000 / 100 shares = $10/share)
        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].purchase_price > 145.0
        expected_adjusted_price = 145.0 + (abs(p._sell_history[-1].gain) / 100)
        assert abs(lots[0].purchase_price - expected_adjusted_price) < 1e-6

    def test_portfolio_no_wash_sale_outside_window(self):
        p = Portfolio(initial_cash=200_000)

        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        p.transaction_date = datetime(2026, 3, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 140.0)

        # 60 days later — outside wash sale window
        p.transaction_date = datetime(2026, 5, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 145.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].purchase_price == 145.0

    def test_wash_sale_no_double_count_across_two_buys(self):
        """One losing sell matched by two successive buys must not double-count."""
        p = Portfolio(initial_cash=500_000)

        # Buy 100 AAPL at $150
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        # Sell 100 AAPL at $140 → $1000 loss (sell_price - cost = 140*100 - 150*100 = -$1000)
        p.transaction_date = datetime(2026, 3, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 140.0)
        loss = p.realized_pnl
        assert loss < 0

        # Buy 100 AAPL at $145, 5 days after sell (within 30-day window)
        p.transaction_date = datetime(2026, 3, 6, tzinfo=UTC)
        p.open_position("AAPL", 100, 145.0)

        # Buy another 100 AAPL at $146, 10 days after sell
        p.transaction_date = datetime(2026, 3, 11, tzinfo=UTC)
        p.open_position("AAPL", 100, 146.0)

        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 2

        total_adjustment = sum(
            (lot.purchase_price - base) * lot.quantity
            for lot, base in [(lots[0], 145.0), (lots[1], 146.0)]
        )
        assert total_adjustment <= abs(loss) + 1e-6

    def test_buy_then_sell_wash_sale_adjusts_existing_lot(self):
        """Buy-then-sell direction: buy first, then sell at loss within 30 days.
        The existing lot's cost basis should be adjusted."""
        p = Portfolio(initial_cash=500_000)

        # Day 0: Buy 100 AAPL at $150
        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 150.0)

        # Day 10: Buy another 100 AAPL at $140 (replacement lot)
        p.transaction_date = datetime(2026, 1, 11, tzinfo=UTC)
        p.open_position("AAPL", 100, 140.0)

        # Day 20: Sell 100 AAPL at $130 → loss.
        # The $140 lot bought 10 days ago is within 30-day window.
        p.transaction_date = datetime(2026, 1, 21, tzinfo=UTC)
        p.close_position("AAPL", 100, 130.0)

        # Remaining lot should have its cost basis adjusted upward
        lots = p.get_tax_lots("AAPL")
        assert len(lots) == 1
        assert lots[0].purchase_price > 150.0 or lots[0].purchase_price > 140.0

    def test_gain_does_not_subtract_tax(self):
        """Gain = proceeds - cost_basis - fees. Tax is NOT subtracted from gain."""
        p = Portfolio(initial_cash=100_000)

        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)

        p.transaction_date = datetime(2026, 2, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 120.0, cost=10.0, tax=500.0)

        sell_proceeds = 100 * 120.0
        cost_basis = 100 * 100.0
        fees = 10.0
        expected_gain = sell_proceeds - cost_basis - fees
        assert abs(p.realized_pnl - expected_gain) < 1e-6

    def test_tax_deducted_from_cash_on_sell(self):
        p = Portfolio(initial_cash=100_000)

        p.transaction_date = datetime(2026, 1, 1, tzinfo=UTC)
        p.open_position("AAPL", 100, 100.0)

        cash_before = p.cash
        p.transaction_date = datetime(2026, 2, 1, tzinfo=UTC)
        p.close_position("AAPL", 100, 120.0, cost=10.0, tax=500.0)

        sell_proceeds = 100 * 120.0
        expected_cash = cash_before + sell_proceeds - 10.0 - 500.0
        assert abs(p.cash - expected_cash) < 1e-6
