"""
Mean Reversion Strategy — Example Plugin

Buys when price drops significantly below the moving average,
sells when it returns to the mean. Factors in costs before trading.

This is a reference implementation showing how to build a
fixed-algorithm strategy plugin.
"""

from __future__ import annotations

import structlog

# When running inside the engine, these come from the engine's modules.
# When developing standalone, they come from the SDK.
try:
    from core.cost_model import ICostModel
    from core.portfolio import PortfolioSnapshot
    from core.signal import Signal
    from plugins.sdk import DataFeed, IStrategy, MarketState, StrategyConfig
except ImportError:
    from nexus_sdk import (
        DataFeed,
        IStrategy,
        MarketState,
        PortfolioSnapshot,
        Signal,
        StrategyConfig,
    )

logger = structlog.get_logger()


class MeanReversionStrategy(IStrategy):
    """
    Simple mean reversion: buy the dip, sell the recovery.
    Fully cost-aware — won't enter trades where costs eat the expected return.
    """

    def __init__(self):
        self._config = None
        self._sma_period = 50
        self._entry_std = 2.0
        self._exit_std = 0.0
        self._position_weight = 0.1
        self._min_net_return_pct = 0.5
        self._watchlist = []

    # ── Identity ──

    @property
    def id(self) -> str:
        return "mean-reversion-basic"

    @property
    def name(self) -> str:
        return "Mean Reversion Basic"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def author(self) -> str:
        return "Nexus Team"

    @property
    def description(self) -> str:
        return "Cost-aware mean reversion strategy"

    # ── Lifecycle ──

    async def initialize(self, config: StrategyConfig) -> None:
        self._config = config
        params = config.params
        self._sma_period = params.get("sma_period", 50)
        self._entry_std = params.get("entry_std", 2.0)
        self._exit_std = params.get("exit_std", 0.0)
        self._position_weight = params.get("position_weight", 0.1)
        self._min_net_return_pct = params.get("min_net_return_pct", 0.5)
        self._watchlist = params.get("watchlist", ["AAPL", "MSFT", "GOOGL", "AMZN", "META"])
        logger.info("mean_reversion.initialized", sma=self._sma_period, entry_std=self._entry_std)

    async def dispose(self) -> None:
        logger.info("mean_reversion.disposed")

    # ── Core evaluation ──

    async def evaluate(
        self,
        portfolio: PortfolioSnapshot,
        market: MarketState,
        costs,  # ICostModel
    ) -> list[Signal]:
        signals = []

        for symbol in self._watchlist:
            price = market.latest(symbol)
            if price is None:
                continue

            sma = market.sma(symbol, period=self._sma_period)
            std = market.std(symbol, period=self._sma_period)
            if sma is None or std is None or std == 0:
                continue

            z_score = (price - sma) / std
            has_position = portfolio.has_position(symbol)

            # ── BUY: price is significantly below the mean ──
            if z_score < -self._entry_std and not has_position:
                # Expected return = distance to mean
                expected_return_pct = ((sma - price) / price) * 100

                # Cost check: only trade if expected return exceeds costs
                cost_pct = costs.estimate_pct(symbol, price, "buy") * 100
                net_return_pct = expected_return_pct - cost_pct

                if net_return_pct >= self._min_net_return_pct:
                    signals.append(Signal.buy(
                        symbol=symbol,
                        strategy_id=self.id,
                        weight=self._position_weight,
                        reason=f"z={z_score:.2f}, exp_net_return={net_return_pct:.2f}%",
                        metadata={"z_score": z_score, "sma": sma, "expected_return": expected_return_pct},
                    ))
                else:
                    logger.debug(
                        "mean_reversion.skip_costly",
                        symbol=symbol,
                        expected=expected_return_pct,
                        cost=cost_pct,
                    )

            # ── SELL: price returned to (or above) the mean ──
            elif z_score > self._exit_std and has_position:
                signals.append(Signal.sell(
                    symbol=symbol,
                    strategy_id=self.id,
                    reason=f"z={z_score:.2f}, returned to mean",
                    metadata={"z_score": z_score, "sma": sma},
                ))

        return signals

    # ── Metadata ──

    def get_config_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "sma_period": {"type": "integer", "default": 50, "min": 10, "max": 200},
                "entry_std": {"type": "number", "default": 2.0},
                "exit_std": {"type": "number", "default": 0.0},
                "position_weight": {"type": "number", "default": 0.1},
                "min_net_return_pct": {"type": "number", "default": 0.5},
            },
        }

    def get_required_data_feeds(self) -> list:
        return [DataFeed(feed_type="ohlcv", symbols=self._watchlist)]

    def get_min_history_bars(self) -> int:
        return self._sma_period + 10

    def get_watchlist(self) -> list[str]:
        return self._watchlist
