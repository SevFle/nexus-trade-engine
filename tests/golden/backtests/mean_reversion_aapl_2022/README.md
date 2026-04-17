# Mean Reversion AAPL 2022

Bollinger Band z-score mean reversion strategy on synthetic AAPL-like
data for 2022. Buys when z-score drops below -2.0, sells when it
rises above 0.5. Tests indicator computation, multiple round-trip
trades, and cost accumulation.

- **Strategy**: MeanReversionStrategy (z-score entry/exit)
- **Data**: Synthetic AAPL-like, seed=201, ~300 bars, sideways market
- **Warmup**: 50 bars
- **Capital**: $100,000 initial
