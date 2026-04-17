# SMA Crossover SPY 50/200 2020-2024

Golden cross / death cross strategy using SMA(50) and SMA(200) on
synthetic SPY-like data. Exercises the MarketState indicator
computations, signal generation on crossover, and round-trip trading
with cost model.

- **Strategy**: SMACrossoverStrategy (SMA 50/200 crossover)
- **Data**: Synthetic SPY-like, seed=102, ~1300 bars
- **Warmup**: 200 bars (SMA(200) requires full window)
- **Capital**: $100,000 initial
