"""Smoke tests that verify core modules are importable and coverage tracks them."""

from __future__ import annotations

import pytest


class TestNexusSdkSmoke:
    def test_import_nexus_sdk_package(self):
        import nexus_sdk

        assert hasattr(nexus_sdk, "__version__")
        assert nexus_sdk.__version__ == "0.1.0"

    def test_money_creation_and_as_pct(self):
        from nexus_sdk.types import Money

        m = Money(amount=42.0)
        assert m.amount == 42.0
        assert m.currency == "USD"
        pct = m.as_pct_of(200.0)
        assert pct == pytest.approx(21.0)

    def test_cost_breakdown_total(self):
        from nexus_sdk.types import CostBreakdown, Money

        cb = CostBreakdown(
            commission=Money(amount=1.0),
            spread=Money(amount=0.5),
        )
        total = cb.total
        assert total.amount == pytest.approx(1.5)

    def test_portfolio_snapshot_summary(self):
        from nexus_sdk.types import PortfolioSnapshot

        snap = PortfolioSnapshot(cash=1000.0, total_value=2500.0)
        summary = snap.summary()
        assert "$2,500.00" in summary
        assert "$1,000.00" in summary

    def test_signal_creation(self):
        from nexus_sdk.signals import Side, Signal

        sig = Signal(symbol="AAPL", side=Side.BUY)
        assert sig.symbol == "AAPL"
        assert sig.side == Side.BUY

    def test_strategy_config(self):
        from nexus_sdk.strategy import StrategyConfig

        cfg = StrategyConfig(strategy_id="test")
        assert cfg.strategy_id == "test"

    def test_import_tax_init(self):
        from engine.core.tax import Trade, TradeSide

        assert Trade is not None
        assert TradeSide is not None

    def test_engine_config_importable(self):
        from engine.config import settings

        assert hasattr(settings, "database_url")
