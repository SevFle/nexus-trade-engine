# Buy-and-Hold SPY 2020-2024

Buys 100 shares of SPY on the first active bar and holds through the
entire period. Tests that equity curve, final capital, and core metrics
remain stable across refactors.

- **Strategy**: BuyHoldStrategy (buys once, holds)
- **Data**: Synthetic SPY-like, seed=101, ~1300 bars
- **Warmup**: 5 bars (minimal)
- **Capital**: $100,000 initial
